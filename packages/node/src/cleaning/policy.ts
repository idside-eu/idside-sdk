// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * An organization's prompt-cleaning policy, read and compiled.
 *
 * Unknown detector ids, actions and custom kinds (written by a newer cloud for
 * a newer engine) are ignored; a malformed policy is refused, never
 * half-applied. Errors name the field or the entry's position, never a value.
 * Python's twin is `_cleaning/policy.py`; the shared vectors hold both.
 */

import { ContentPolicyError } from "../exceptions.ts";
import { DETECTORS, LIMITS, WORD_CLASS, boundary, escapeRegExp, type Pattern } from "./rules.ts";
import { fold, mapCodePoint } from "./shadow.ts";
import { VALIDATORS } from "./validators.ts";

const ACTIONS = new Set(["mask", "block"]);
const KINDS = new Set(["term", "domain", "pattern"]);
const LABEL = /^[A-Z0-9_]{1,24}$/;
const DOMAIN = /^[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){1,8}$/;
const WORD = new RegExp("^[" + WORD_CLASS + "]$");
const SPACES = /[ \t\r\n\f\v]+/;
const RUN = LIMITS.pattern_run;

/** One rule as the engine runs it: a detector, or a custom entry. */
export interface Rule {
  /** What counts and blocks are reported under: a detector id or `custom:LABEL`. */
  readonly key: string;
  readonly label: string;
  readonly action: string;
  /** Tie-break after length: custom entries first, in policy order, then the catalogue. */
  readonly order: number;
  readonly patterns: readonly Pattern[];
  readonly validator: ((value: string) => number | undefined) | undefined;
  /** Read on the ASCII-lowered shadow (custom terms and domains). */
  readonly lowered: boolean;
}

export interface CompiledPolicy {
  readonly rules: readonly Rule[];
  readonly blockAttachments: boolean;
}

function refuse(what: string): ContentPolicyError {
  return new ContentPolicyError(`The prompt-cleaning policy cannot be applied: ${what}. The AI call was not made.`);
}

function pattern(source: string, prefilter: readonly string[], extend?: string): Pattern {
  return {
    regex: new RegExp(source, "gd"),
    group: 1,
    keywords: undefined,
    prefilter,
    extend: extend === undefined ? undefined : new RegExp(extend, "y"),
    blockOnly: false,
    followedBy: undefined,
  };
}

/** Code points, as Python counts a string's length. */
export function codePoints(text: string): number {
  let surrogatePairs = 0;
  for (let index = 0; index < text.length - 1; index++) {
    const high = text.charCodeAt(index);
    if (high >= 0xd800 && high <= 0xdbff) {
      const low = text.charCodeAt(index + 1);
      if (low >= 0xdc00 && low <= 0xdfff) {
        surrogatePairs += 1;
        index += 1;
      }
    }
  }
  return text.length - surrogatePairs;
}

function stripSpaces(text: string): string {
  return text.replace(/^[ \t\r\n\f\v]+|[ \t\r\n\f\v]+$/g, "");
}

/** The first longest string, as Python's `max(…, key=len)` picks it. */
function longest(items: readonly string[]): string {
  return items.reduce((best, item) => (item.length > best.length ? item : best), items[0]);
}

function term(value: string, where: string): Pattern[] {
  const words = fold(value)
    .split(SPACES)
    .filter((word) => word.length > 0);
  if (words.length === 0) throw refuse(`${where} has no visible character`);
  const body = words.map(escapeRegExp).join("[ \\t\\r\\n]{1,16}");
  const lead = WORD.test(words[0][0]) ? boundary(WORD_CLASS) : "";
  const last = words[words.length - 1];
  const trail = WORD.test(last[last.length - 1]) ? "(?![" + WORD_CLASS + "])" : "";
  return [pattern(lead + "(" + body + ")" + trail, [longest(words)])];
}

function domain(value: string, where: string): Pattern[] {
  let name = stripSpaces(fold(value));
  for (const prefix of ["@", "*."]) if (name.startsWith(prefix)) name = name.slice(prefix.length);
  name = name.replace(/^\.+|\.+$/g, "");
  if (!DOMAIN.test(name)) throw refuse(`${where} is not a domain name`);
  const escaped = escapeRegExp(name);
  const host = "(?:[a-z0-9-]{1,63}\\.){0,8}" + escaped + "(?![a-z0-9-]|\\.[a-z0-9])";
  const url = boundary("a-z0-9+.-") + "([a-z][a-z0-9+.-]{1,30}://(?:[^/@ \\t\\r\\n\"'<>]{0,256}@)?" + host;
  return [
    // An e-mail address at the domain or one of its subdomains.
    pattern(boundary("a-z0-9._%+-") + "([a-z0-9._%+-]{1,64}@" + host + ")", [name]),
    // A URL whose host is the domain, and with a port, a path, a query or a
    // fragment, all of it to the next blank or quote.
    pattern(url + ")", [name]),
    pattern(url + "[:/?#])", [name], "[^ \\t\\r\\n\"'<>]{1,4096}"),
    // The host name alone.
    pattern(boundary("a-z0-9.@/_-") + "(" + host + ")", [name]),
  ];
}

function customPattern(value: string, where: string): Pattern[] {
  if (value.includes("**")) throw refuse(`${where} has two \`*\` in a row`);
  const tokens: [string, boolean][] = []; // [regex, can match a letter or digit]
  const literals: string[] = [];
  let literal = "";
  let stars = 0;
  for (const char of value) {
    if (char === "#" || char === "@" || char === "*") {
      literals.push(literal);
      literal = "";
    }
    if (char === "#") tokens.push(["[0-9]", true]);
    else if (char === "@") tokens.push(["[A-Za-z]", true]);
    else if (char === "*") {
      stars += 1;
      // A run is atomic: it takes every letter and digit there is (up to the
      // limit), so nothing ever backtracks into it.
      tokens.push([`(?=(?<run${stars}>[A-Za-z0-9]{1,${RUN}}))\\k<run${stars}>`, true]);
    } else {
      for (const mapped of mapCodePoint(char.codePointAt(0) as number)) {
        if (/^[A-Za-z]$/.test(mapped)) {
          tokens.push([`[${mapped.toUpperCase()}${mapped.toLowerCase()}]`, true]);
          literal += mapped.toLowerCase();
        } else {
          const digit = /^[0-9]$/.test(mapped);
          tokens.push([escapeRegExp(mapped), digit]);
          if (digit) literal += mapped;
          else {
            literals.push(literal);
            literal = "";
          }
        }
      }
    }
  }
  literals.push(literal);
  if (tokens.length === 0) throw refuse(`${where} has no visible character`);
  for (let index = 0; index + 1 < tokens.length; index++) {
    // A run takes every letter and digit there is: one right after it could
    // never be matched, so the pattern never would be.
    if (tokens[index][0].startsWith("(?=(?<run") && tokens[index + 1][1]) {
      throw refuse(`${where} has a \`*\` followed by a letter, a digit, \`#\`, \`@\` or \`*\``);
    }
  }
  const lead = tokens[0][1] ? boundary("A-Za-z0-9") : "";
  const trail = tokens[tokens.length - 1][1] ? "(?![A-Za-z0-9])" : "";
  const best = longest(literals);
  return [pattern(lead + "(" + tokens.map(([regex]) => regex).join("") + ")" + trail, best.length >= 2 ? [best] : [])];
}

const BUILDERS: Record<string, (value: string, where: string) => Pattern[]> = {
  term,
  domain,
  pattern: customPattern,
};

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function compilePolicy(policy: unknown): CompiledPolicy {
  if (!isObject(policy)) throw refuse("it is not an object");
  if (policy.schema !== 1) throw refuse("its `schema` is not 1, the only one this SDK reads");
  // Absent means the default; present but null is malformed, as in Python.
  const detectors = policy.detectors === undefined ? {} : policy.detectors;
  if (!isObject(detectors)) throw refuse("`detectors` is not an object");
  const attachments = policy.attachments === undefined ? "allow" : policy.attachments;
  if (typeof attachments !== "string") throw refuse("`attachments` is not a string");
  const custom = policy.custom === undefined ? [] : policy.custom;
  if (!Array.isArray(custom)) throw refuse("`custom` is not a list");
  if (custom.length > LIMITS.custom_entries) throw refuse(`\`custom\` has more than ${LIMITS.custom_entries} entries`);

  const rules: Rule[] = [];
  custom.forEach((entry: unknown, position: number) => {
    const where = `custom entry ${position}`;
    if (!isObject(entry)) throw refuse(`${where} is not an object`);
    const { kind, action, label, value } = entry;
    if (typeof kind !== "string" || !KINDS.has(kind) || typeof action !== "string" || !ACTIONS.has(action)) {
      return; // written by a newer cloud for a newer engine
    }
    if (typeof label !== "string" || !LABEL.test(label)) throw refuse(`${where} has a label outside [A-Z0-9_]{1,24}`);
    if (typeof value !== "string" || codePoints(value) < 1 || codePoints(value) > LIMITS.custom_value_chars) {
      throw refuse(`${where} has a value that is not 1 to ${LIMITS.custom_value_chars} characters`);
    }
    rules.push({
      key: `custom:${label}`,
      label,
      action,
      order: position,
      patterns: BUILDERS[kind](value, where),
      validator: undefined,
      lowered: kind !== "pattern",
    });
  });

  for (const [detectorId, action] of Object.entries(detectors)) {
    if (typeof action !== "string") throw refuse(`the action of detector ${JSON.stringify(detectorId)} is not a string`);
    const detector = DETECTORS.get(detectorId);
    if (detector === undefined || !ACTIONS.has(action)) continue; // a newer catalogue id, or a newer action
    rules.push({
      key: detector.id,
      label: detector.label,
      action,
      order: LIMITS.custom_entries + detector.index,
      patterns: action === "block" ? detector.patterns : detector.patterns.filter((pattern) => !pattern.blockOnly),
      validator: detector.validator ? VALIDATORS[detector.validator] : undefined,
      lowered: false,
    });
  }
  rules.sort((a, b) => a.order - b.order);
  return { rules, blockAttachments: attachments !== "allow" };
}
