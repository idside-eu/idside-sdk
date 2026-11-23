// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * The prompt-cleaning engine: walk a call's body, mask or block what the policy names.
 *
 * Deterministic and shared with the Python SDK (`_cleaning/engine.py`); the
 * vectors in `packages/descriptors/testdata/content-cleaning-vectors.json`
 * hold both to the same output, counts and blocks, byte for byte. The engine
 * never logs, throws or returns a matched value.
 */

import { NO_CONTENT_FIELDS, type ContentFields } from "../descriptors.ts";
import { compilePolicy, codePoints, type CompiledPolicy, type Rule } from "./policy.ts";
import { CATALOGUE_IDS, LIMITS, PLACEHOLDER } from "./rules.ts";
import { shadowOf, type Shadow } from "./shadow.ts";

/** A base64 data: URI is an attachment wherever it stands; it is not decoded. */
const DATA_URI =
  /^[Dd][Aa][Tt][Aa]:[A-Za-z0-9.+-]{1,64}\/[A-Za-z0-9.+-]{1,128}(?:;[A-Za-z0-9=._+-]{1,128}){0,8};[Bb][Aa][Ss][Ee]64,[A-Za-z0-9+/=\r\n]*$/;
const DIGIT = /[0-9]/;

/** The id under which a call blocked by the engine's own limits is reported. */
export const SIZE_LIMIT = "size_limit";
/** The id under which a call blocked for carrying an attachment is reported. */
export const ATTACHMENTS = "attachments";

/**
 * What cleaning one call produced. Never holds a matched value.
 *
 * `payload` is the body that may leave (the input itself when nothing changed,
 * and when the call is blocked). `counts`: masked occurrences per detector id
 * or `custom:LABEL`. `blocked`: when not empty, the call must not be sent.
 */
export interface CleanResult {
  readonly payload: unknown;
  readonly counts: Readonly<Record<string, number>>;
  readonly blocked: Readonly<Record<string, number>>;
  readonly attachments: number;
}

class LimitReached extends Error {}

/**
 * The highest placeholder number: the placeholder pattern reads nine digits. A
 * text that already holds it cannot be numbered further without two values
 * sharing a placeholder, so the call is blocked instead.
 */
const LAST_NUMBER = 999_999_999;

interface Candidate {
  readonly start: number; // shadow offsets
  readonly end: number;
  readonly rule: Rule;
}

type Path = readonly (string | number)[];

/** Sorted, non-overlapping intervals. */
class Spans {
  private readonly starts: number[] = [];
  private readonly ends: number[] = [];

  private bisect(value: number): number {
    let low = 0;
    let high = this.starts.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (this.starts[middle] < value) low = middle + 1;
      else high = middle;
    }
    return low;
  }

  overlaps(start: number, end: number): boolean {
    const index = this.bisect(start);
    if (index > 0 && this.ends[index - 1] > start) return true;
    return index < this.starts.length && this.starts[index] < end;
  }

  add(start: number, end: number): void {
    const index = this.bisect(start);
    this.starts.splice(index, 0, start);
    this.ends.splice(index, 0, end);
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function present(value: unknown): boolean {
  if (value === null || value === "") return false; // undefined never gets here: the walk skips it
  if (Array.isArray(value)) return value.length > 0;
  if (isObject(value)) return Object.keys(value).length > 0;
  return true;
}

function attachmentObject(fields: ContentFields, key: string, value: unknown): boolean {
  if (!isObject(value)) return false;
  const kind = value.type;
  const types = fields.attachmentObjects.get(key);
  return types !== undefined && typeof kind === "string" && types.has(kind);
}

/**
 * Where a value that runs on ends: `extend` (sticky) applied at its end, again
 * and again, while it takes a character. Each step is bounded and the search
 * goes on from the end found, so every character is read once: linear,
 * whatever the length of the value, and never cut short.
 */
function extendValue(extend: RegExp, source: string, end: number): number {
  for (;;) {
    extend.lastIndex = end;
    if (extend.exec(source) === null || extend.lastIndex <= end) return end;
    end = extend.lastIndex;
  }
}

/** Every keyword hit (shadow offsets) outside placeholders. */
function keywordHits(keywords: RegExp, shadow: Shadow, placeholders: Spans): [number[], number[]] {
  const starts: number[] = [];
  const ends: number[] = [];
  keywords.lastIndex = 0;
  for (let match = keywords.exec(shadow.lower); match; match = keywords.exec(shadow.lower)) {
    const [start, end] = (match.indices as RegExpIndicesArray)[1];
    if (!placeholders.overlaps(start, end)) {
      starts.push(start);
      ends.push(end);
    }
  }
  return [starts, ends];
}

/** Whether a keyword hit lies within `window` characters of [start, end). */
function near([starts, ends]: [number[], number[]], start: number, end: number, window: number): boolean {
  let low = 0;
  let high = starts.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (starts[middle] < end + window) low = middle + 1;
    else high = middle;
  }
  return low > 0 && ends[low - 1] > start - window;
}

/**
 * Longest first, then rule order, then position; an overlap is absorbed.
 *
 * A candidate that overlaps those already kept is not dropped: the kept one
 * grows to cover it, under the kept one's rule, so no part of a value found
 * stays in the clear. Lengths are shadow lengths, the same in both SDKs;
 * overlaps are judged on the original characters, since two shadow spans can
 * come from one original character (a ligature).
 *
 * Taken in that order, what is kept in the end is each group of candidates
 * that overlap one another, directly or through others of the group: the
 * whole group covered, under the rule of the first of them in that order. So
 * the groups are read off once the candidates are sorted by where they start,
 * rather than by inserting each into a sorted list, which took quadratic time
 * on a text of many short findings.
 */
export function resolveCandidates(candidates: readonly Candidate[], shadow: Shadow): Candidate[] {
  const ordered = [...candidates].sort(
    (a, b) => b.end - b.start - (a.end - a.start) || a.rule.order - b.rule.order || a.start - b.start,
  );
  // Original start and end, rank and candidate; sorted by where they start.
  const spans = ordered
    .map((candidate, rank): [number, number, number, Candidate] => [
      shadow.start(candidate.start),
      shadow.end(candidate.end),
      rank,
      candidate,
    ])
    .sort((a, b) => a[0] - b[0]);
  const resolved: Candidate[] = [];
  let index = 0;
  while (index < spans.length) {
    let [, reach, rank, winner] = spans[index];
    let low = winner.start;
    let high = winner.end;
    index += 1;
    // Original spans are never empty, so one that starts before the group
    // ends overlaps one of its candidates.
    while (index < spans.length && spans[index][0] < reach) {
      const [, end, otherRank, other] = spans[index];
      reach = Math.max(reach, end);
      low = Math.min(low, other.start);
      high = Math.max(high, other.end);
      if (otherRank < rank) {
        rank = otherRank;
        winner = other;
      }
      index += 1;
    }
    resolved.push(low === winner.start && high === winner.end ? winner : { start: low, end: high, rule: winner.rule });
  }
  return resolved;
}

function rebuild(payload: unknown, replacements: Map<Path, string>): unknown {
  if (replacements.size === 0) return payload;
  type Tree = Map<string | number, Tree | string>;
  const tree: Tree = new Map();
  for (const [path, text] of replacements) {
    if (path.length === 0) return text;
    let node = tree;
    for (const step of path.slice(0, -1)) {
      let next = node.get(step);
      if (next === undefined) {
        next = new Map();
        node.set(step, next);
      }
      node = next as Tree;
    }
    node.set(path[path.length - 1], text);
  }
  const apply = (node: unknown, changes: Tree): unknown => {
    const copy = (Array.isArray(node) ? [...node] : { ...(node as object) }) as Record<string | number, unknown>;
    for (const [step, change] of changes) {
      copy[step] = typeof change === "string" ? change : apply((node as Record<string | number, unknown>)[step], change);
    }
    return copy;
  };
  return apply(payload, tree);
}

/** A compiled policy and a provider's content fields, ready to clean bodies. */
export class ContentCleaner {
  private readonly policy: CompiledPolicy;
  private readonly fields: ContentFields;

  constructor(policy: unknown, fields: ContentFields = NO_CONTENT_FIELDS) {
    this.policy = compilePolicy(policy);
    this.fields = fields;
  }

  /** Every catalogue detector masking: the policy challenges are answered with. */
  static reference(): ContentCleaner {
    return new ContentCleaner(referencePolicy());
  }

  private walk(
    node: unknown,
    path: Path,
    depth: number,
    texts: [Path, string][],
    attachments: { count: number },
    free: boolean,
    named = false,
  ): void {
    // `named`: this object maps names the caller chose to values (a schema's
    // `properties`, a prompt's `variables`). Its own keys are names, never
    // structural nor attachment keys; each value is read with the usual
    // rules, from the next level down.
    if (depth > LIMITS.max_depth) throw new LimitReached();
    if (typeof node === "string") {
      if (DATA_URI.test(node)) attachments.count += 1;
      else texts.push([path, node]);
    } else if (Array.isArray(node)) {
      node.forEach((item, index) => this.walk(item, [...path, index], depth + 1, texts, attachments, free));
    } else if (isObject(node)) {
      const fields = this.fields;
      // Default string order is UTF-16 code units: RFC 8785's, and Python's here.
      for (const key of Object.keys(node).sort()) {
        const value = node[key];
        if (value === undefined) continue; // absent, as JSON writes it
        const inner = free || (!named && fields.freeFormKeys.has(key));
        if (!(free || named)) {
          if (fields.structuralKeys.has(key)) continue;
          if (fields.attachmentKeys.has(key) || attachmentObject(fields, key, value)) {
            attachments.count += present(value) ? 1 : 0;
            continue;
          }
        }
        this.walk(
          value,
          [...path, key],
          depth + 1,
          texts,
          attachments,
          inner,
          !(inner || named) && fields.callerKeyedKeys.has(key),
        );
      }
    } else if (!(node === null || node === undefined || typeof node === "boolean" || typeof node === "number")) {
      throw new TypeError(
        `cannot clean a value of type ${typeof node}: the body must be JSON (object, array, string, number, boolean, null)`,
      );
    }
  }

  private find(shadow: Shadow, placeholders: Spans): [Candidate[], Candidate[]] {
    const masks: Candidate[] = [];
    const blocks: Candidate[] = [];
    let hasDigit: boolean | undefined;
    const window = LIMITS.keyword_window;
    for (const rule of this.policy.rules) {
      const source = rule.lowered ? shadow.lower : shadow.text;
      for (const pattern of rule.patterns) {
        if (pattern.prefilter.length > 0) {
          let wanted = false;
          for (const literal of pattern.prefilter) {
            if (literal === "#digit") {
              if (hasDigit === undefined) hasDigit = DIGIT.test(shadow.text);
              wanted = hasDigit;
            } else {
              wanted = shadow.lower.includes(literal);
            }
            if (wanted) break;
          }
          if (!wanted) continue;
        }
        let hits: [number[], number[]] | undefined;
        const regex = pattern.regex;
        regex.lastIndex = 0;
        for (let match = regex.exec(source); match; match = regex.exec(source)) {
          const span = (match.indices as RegExpIndicesArray)[pattern.group];
          if (span === undefined) {
            // The value group took no part: the pattern matched the context
            // that rules a value out. Skip it whole.
            regex.lastIndex = Math.max(match.index + match[0].length, match.index + 1);
            continue;
          }
          if (pattern.followedBy !== undefined && !pattern.followedBy(source, match.index + match[0].length)) {
            // What must follow is not there: no match starts here, as if the
            // expression had said so.
            regex.lastIndex = match.index + 1;
            continue;
          }
          let [start, end] = span;
          let accepted = end > start && !placeholders.overlaps(start, end);
          if (accepted && pattern.keywords !== undefined) {
            hits ??= keywordHits(pattern.keywords, shadow, placeholders);
            accepted = near(hits, start, end, window);
          }
          if (accepted && rule.validator !== undefined) {
            const length = rule.validator(source.slice(start, end));
            accepted = length !== undefined;
            if (length !== undefined) end = start + length;
          }
          if (accepted) {
            if (pattern.extend !== undefined) end = extendValue(pattern.extend, source, end);
            (rule.action === "block" ? blocks : masks).push({ start, end, rule });
            regex.lastIndex = end;
          } else {
            regex.lastIndex = start + 1;
          }
        }
      }
    }
    return [masks, blocks];
  }

  /** Clean one call's body. Never mutates it. */
  clean(payload: unknown): CleanResult {
    const texts: [Path, string][] = [];
    const attachments = { count: 0 };
    try {
      this.walk(payload, [], 0, texts, attachments, false);
      let total = 0;
      for (const [, text] of texts) total += text.length;
      if (total > LIMITS.max_text_chars) {
        // Code points, as Python counts them: only worth counting past the limit.
        total = 0;
        for (const [, text] of texts) total += codePoints(text);
        if (total > LIMITS.max_text_chars) throw new LimitReached();
      }
    } catch (error) {
      if (error instanceof LimitReached) return { payload, counts: {}, blocked: { [SIZE_LIMIT]: 1 }, attachments: attachments.count };
      throw error;
    }

    const blocked: Record<string, number> = {};
    if (attachments.count > 0 && this.policy.blockAttachments) blocked[ATTACHMENTS] = attachments.count;

    const current = texts.map(([, text]) => text);
    const shadows = current.map(shadowOf);
    const floors = new Map<string, number>();
    for (const shadow of shadows) {
      for (const match of shadow.text.matchAll(PLACEHOLDER)) {
        const inner = match[0].slice(1, -1);
        const cut = inner.lastIndexOf("_");
        const label = inner.slice(0, cut);
        floors.set(label, Math.max(floors.get(label) ?? 0, Number(inner.slice(cut + 1))));
      }
    }

    const counts: Record<string, number> = {};
    const assigned = new Map<string, string>();
    const following = new Map<string, number>();
    let todo = current.map((_, index) => index);
    let converged = false;
    for (let round = 0; round < LIMITS.max_passes; round++) {
      const changed: number[] = [];
      const foundBlocks: Record<string, number> = {};
      let blocking = false;
      for (const index of todo) {
        const shadow = round === 0 ? shadows[index] : shadowOf(current[index]);
        const placeholders = new Spans();
        for (const match of shadow.text.matchAll(PLACEHOLDER)) placeholders.add(match.index, match.index + match[0].length);
        const [masks, blocks] = this.find(shadow, placeholders);
        for (const candidate of resolveCandidates(blocks, shadow)) {
          foundBlocks[candidate.rule.key] = (foundBlocks[candidate.rule.key] ?? 0) + 1;
          blocking = true;
        }
        if (blocking || masks.length === 0) continue;
        try {
          current[index] = replace(current[index], shadow, resolveCandidates(masks, shadow), {
            assigned,
            following,
            floors,
            counts,
          });
        } catch (error) {
          if (error instanceof LimitReached) return { payload, counts: {}, blocked: { [SIZE_LIMIT]: 1 }, attachments: attachments.count };
          throw error;
        }
        changed.push(index);
      }
      if (blocking) {
        for (const [key, count] of Object.entries(foundBlocks)) blocked[key] = (blocked[key] ?? 0) + count;
        converged = true;
        break;
      }
      if (changed.length === 0) {
        converged = true;
        break;
      }
      todo = changed;
    }
    if (!converged) return { payload, counts: {}, blocked: { [SIZE_LIMIT]: 1 }, attachments: attachments.count };

    if (Object.keys(blocked).length > 0) return { payload, counts: {}, blocked, attachments: attachments.count };
    const replacements = new Map<Path, string>();
    texts.forEach(([path, original], index) => {
      if (current[index] !== original) replacements.set(path, current[index]);
    });
    return { payload: rebuild(payload, replacements), counts, blocked: {}, attachments: attachments.count };
  }
}

interface Numbering {
  readonly assigned: Map<string, string>;
  readonly following: Map<string, number>;
  readonly floors: Map<string, number>;
  readonly counts: Record<string, number>;
}

function replace(text: string, shadow: Shadow, masks: readonly Candidate[], numbering: Numbering): string {
  const { assigned, following, floors, counts } = numbering;
  const pieces: string[] = [];
  let position = 0;
  for (const candidate of masks) {
    const label = candidate.rule.label;
    const value = label + "\u0000" + shadow.lower.slice(candidate.start, candidate.end);
    let placeholder = assigned.get(value);
    if (placeholder === undefined) {
      const number = following.get(label) ?? (floors.get(label) ?? 0) + 1;
      if (number > LAST_NUMBER) throw new LimitReached();
      following.set(label, number + 1);
      placeholder = `[${label}_${number}]`;
      assigned.set(value, placeholder);
    }
    counts[candidate.rule.key] = (counts[candidate.rule.key] ?? 0) + 1;
    const start = shadow.start(candidate.start);
    pieces.push(text.slice(position, start), placeholder);
    position = shadow.end(candidate.end);
  }
  pieces.push(text.slice(position));
  return pieces.join("");
}

export function referencePolicy(): Record<string, unknown> {
  return {
    schema: 1,
    app_id: "",
    revision: 0,
    detectors: Object.fromEntries(CATALOGUE_IDS.map((id) => [id, "mask"])),
    attachments: "allow",
    custom: [],
  };
}
