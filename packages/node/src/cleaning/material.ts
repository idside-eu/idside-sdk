// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Whether key material follows a private key's BEGIN line: the check a pattern names.
 *
 * A private key without its END line is taken only when key material comes
 * right after its BEGIN line (`content-rules.json`, `private_key`): between
 * them stand only separators (blanks, line breaks real or escaped, line
 * prefixes, quotes, `<br>`, placeholders), at most 1,024 of them, then up to
 * four PEM or PGP header lines (`Comment:` and its value, at most 256
 * characters, then at most 512 separators), then 32 base64 characters in one
 * run or in two, the first of 16 or more, with 1 to 1,024 separators between
 * them.
 *
 * That reading was a look-ahead in the pattern. A backtracking matcher tries
 * every place a header value may end, and every one of them again for each
 * later header line: four lines of `<br>` took a minute. Here the same reading
 * is done once: the places a value may end are read together, each position
 * after them is reached with the fewest separators any reading takes to get
 * there, and the separator runs themselves are walked by one bounded
 * expression. Every reading the look-ahead accepts is accepted here, and no
 * other: the vectors and the differential tests hold both to the same answer.
 *
 * Written alike in the Python SDK (`_cleaning/material.py`).
 */

/** One separator: a blank, a line break real or escaped, a line prefix, a quote, `//`, `<br>` or a placeholder. */
const SEPARATOR = "(?:[ \\t\\r\\n>+#;*|\"',«»-]|\\\\[nrt]?|//|<[Bb][Rr] ?/?>|\\[[A-Z0-9_]{1,24}_[0-9]{1,9}\\])";
const TOKEN = new RegExp(SEPARATOR, "y");
/** The separators that are one character, every one of them. */
const SINGLES = " \t\r\n>+#;*|\"',«»-";
const HEADER_NAMES = ["Proc-Type", "DEK-Info", "Version", "Comment", "Hash", "Charset", "MessageID"];
const HEADER_SOURCE = HEADER_NAMES.map((name) => name + ":").join("|");
const HEADER_AT = new RegExp(HEADER_SOURCE, "y");
const HEADER_FROM = new RegExp(HEADER_SOURCE, "g");
/** The longest header name and its colon: how far past a position a header that starts there reaches. */
const HEADER_REACH = Math.max(...HEADER_NAMES.map((name) => name.length + 1));
/** A value never holds a line break, real or escaped (the backslash). */
const LINE_END = /[\r\n\\]/g;
/** Characters a header value may not end on. */
const NOT_LAST = new Set("\r\n\\ \t>+#;*|\"',«»<-");
const BASE64 = /[A-Za-z0-9+/]{0,32}/y;
const FIRST = /[A-Za-z0-9/]/;
/** A run of 16 base64 characters or more (key material starts in one). */
const RUN = /[A-Za-z0-9+/]{16,}/g;
const FIRST_IN_RUN = /[A-Za-z0-9/]/g;

/** The bounds of the reading (see above). */
const LEAD = 1024;
const HEADERS = 4;
const VALUE = 256;
const AFTER_HEADER = 512;
const MATERIAL = 32;
const FIRST_RUN = 16;
const BETWEEN_RUNS = 1024;

const CHAINS = new Map<number, RegExp>();
const STARTS = new Map<number, RegExp>();

/**
 * Up to `most` separators, one after another: the one way a run of them reads
 * (an escape takes its letter; the letter alone is a side branch).
 */
function chain(most: number): RegExp {
  let pattern = CHAINS.get(most);
  if (pattern === undefined) {
    pattern = new RegExp("(?:" + SEPARATOR + "){0," + most + "}", "y");
    CHAINS.set(most, pattern);
  }
  return pattern;
}

/**
 * Where, inside a run of separators, a base64 run of `run` characters or more
 * may start: on the letter of an escape (`\n` read as `\` then `n...`), or on
 * `//` that no `/` comes before (a later `/` of the same slashes starts a
 * shorter run).
 */
function starts(run: number): RegExp {
  let pattern = STARTS.get(run);
  if (pattern === undefined) {
    pattern = new RegExp(
      "\\\\(?=[nrt][A-Za-z0-9+/]{" + (run - 1) + "})|(?<!/)(?=//[A-Za-z0-9+/]{" + Math.max(run - 2, 0) + "})",
      "g",
    );
    STARTS.set(run, pattern);
  }
  return pattern;
}

/** Where `pattern` (sticky) matches at `at` ends, or -1. */
function matchAt(pattern: RegExp, text: string, at: number): number {
  pattern.lastIndex = at;
  return pattern.exec(text) === null ? -1 : pattern.lastIndex;
}

/** Where `pattern` (global) first matches in [`from`, `to`), the text read up to `to` only; or -1. */
function search(pattern: RegExp, text: string, from: number, to: number): number {
  const part = to < text.length ? text.slice(0, to) : text;
  pattern.lastIndex = from;
  const match = pattern.exec(part);
  return match === null ? -1 : match.index;
}

/** The base64 characters from `at`, counted up to 32. */
function run(text: string, at: number): number {
  return matchAt(BASE64, text, at) - at;
}

/** One reading of the text after one BEGIN line. */
class Reader {
  /** (where the separators start, how long the second run must be) -> whether one is reached. */
  private readonly seconds = new Map<string, boolean>();
  private readonly text: string;

  constructor(text: string) {
    this.text = text;
  }

  // ── key material ──────────────────────────────────────────────────────

  /**
   * Whether 32 base64 characters, in one run or two, start at `at`.
   *
   * Two runs are read only where this run ends: a + or // inside it cannot
   * split it, since a second run inside the same characters would make 33 of
   * them, and one run of 32 is already key material.
   */
  material(at: number): boolean {
    const text = this.text;
    if (at >= text.length || !FIRST.test(text[at])) return false;
    const length = run(text, at);
    if (length >= MATERIAL) return true;
    return length >= FIRST_RUN && this.second(at + length, MATERIAL - length);
  }

  /** Whether 1 to 1,024 separators from `at` lead to `length` base64 characters or more, the first a letter, a digit or /. */
  private second(at: number, length: number): boolean {
    const key = at + ":" + length;
    let found = this.seconds.get(key);
    if (found === undefined) {
      found = this.findSecond(at, length);
      this.seconds.set(key, found);
    }
    return found;
  }

  private findSecond(at: number, length: number): boolean {
    // `at` follows a run of base64, so it is not base64 itself.
    const text = this.text;
    const end = matchAt(chain(BETWEEN_RUNS), text, at);
    if (end === at) return false;
    if (end < text.length && FIRST.test(text[end]) && run(text, end) >= length) return true;
    const hit = search(starts(length), text, at, end + MATERIAL);
    return hit !== -1 && hit < end;
  }

  // ── separators ────────────────────────────────────────────────────────

  /** Follow at most `most` separators from `at`: whether key material starts on the way, and where the separators end. */
  walk(at: number, most: number): [boolean, number] {
    const text = this.text;
    const end = matchAt(chain(most), text, at);
    if (this.material(at) || (end > at && this.material(end))) return [true, end];
    const pattern = starts(FIRST_RUN);
    let hit = search(pattern, text, at, end + MATERIAL);
    while (hit !== -1 && hit < end) {
      // An escape's letter, or the first / of slashes.
      if (this.material(text[hit] === "\\" ? hit + 1 : hit)) return [true, end];
      hit = search(pattern, text, hit + 1, end + MATERIAL);
    }
    return [false, end];
  }

  // ── a header line ─────────────────────────────────────────────────────

  /**
   * The header value starting at `at` and the separators after it: whether
   * key material starts there, and where the value of a next header may start.
   */
  header(at: number): [boolean, number[]] {
    const text = this.text;
    let limit = Math.min(at + VALUE, text.length);
    const lineEnd = search(LINE_END, text, at, limit);
    if (lineEnd !== -1) limit = lineEnd;
    const header = search(HEADER_FROM, text, at, limit + HEADER_REACH);
    if (header !== -1 && header < limit) limit = header;
    // `limit`: the value ends at or before it. Every position up to it that a
    // character other than a separator or < precedes is the end of a value,
    // reached with no separator at all.
    const runs = text.slice(0, Math.min(limit + FIRST_RUN, text.length));
    RUN.lastIndex = at;
    // A run read up to 16 characters past the bound starts at or before it.
    for (let found = RUN.exec(runs); found !== null; found = RUN.exec(runs)) {
      const stop = Math.min(found.index + found[0].length, limit + 1);
      // The first place in the run that is reached and may start a run of
      // base64: a later one starts a shorter run of the same characters.
      let where = search(FIRST_IN_RUN, text, found.index, stop);
      while (where !== -1) {
        if (this.reached(at, where) !== undefined) {
          if (this.material(where)) return [true, []];
          break;
        }
        where = search(FIRST_IN_RUN, text, where + 1, stop);
      }
      RUN.lastIndex = found.index + found[0].length;
    }
    const heads: [number, number][] = [];
    const used = this.reached(at, limit);
    if (used !== undefined) heads.push([limit, used]);
    if (limit === at + VALUE) {
      // A separator that starts before the bound and ends after it.
      for (let start = Math.max(at, limit - 35); start < limit; start++) {
        if ("</[".includes(text[start])) {
          const end = matchAt(TOKEN, text, start);
          if (end > limit) {
            const before = this.reached(at, start);
            if (before !== undefined) heads.push([end, before + 1]);
          }
        }
      }
    }
    const found: number[] = [];
    for (const [head, before] of heads) {
      const [material, end] = this.walk(head, AFTER_HEADER - before);
      if (material) return [true, []];
      const value = matchAt(HEADER_AT, text, end);
      if (value !== -1) found.push(value);
    }
    return [false, found];
  }

  /**
   * The fewest separators that reach `where` (`at` <= `where` <= the end of the
   * value) from the header value at `at`, or `undefined`.
   *
   * A position after a character a value may end on is reached with none.
   * After separators of one character back to such a position (or to `at`),
   * with one per character, but for one shortcut: `<br >` whose `<` is itself
   * reached with none. A `<` in between, which only `<br>` reads, leaves it
   * unreached.
   */
  private reached(at: number, where: number): number | undefined {
    const text = this.text;
    if (where === at || !NOT_LAST.has(text[where - 1])) return 0;
    let start = where;
    while (start > at && SINGLES.includes(text[start - 1])) start -= 1;
    if (start > at && text[start - 1] === "<") return undefined;
    let used = where - start;
    const tag = start - 3;
    if (
      used >= 2 &&
      tag >= at &&
      text.startsWith(" >", start) &&
      text[tag] === "<" &&
      "Bb".includes(text[tag + 1]) &&
      "Rr".includes(text[tag + 2]) &&
      (tag === at || !NOT_LAST.has(text[tag - 1]))
    ) {
      used -= 1;
    }
    return used;
  }
}

/** Whether key material follows the BEGIN line that ends at `at`. */
export function privateKeyMaterial(text: string, at: number): boolean {
  const reader = new Reader(text);
  const [material, end] = reader.walk(at, LEAD);
  if (material) return true;
  const name = matchAt(HEADER_AT, text, end);
  let values = name === -1 ? [] : [name];
  for (let line = 0; line < HEADERS; line++) {
    const following: number[] = [];
    for (const value of new Set(values)) {
      const [found, after] = reader.header(value);
      if (found) return true;
      following.push(...after);
    }
    values = following;
  }
  return false;
}
