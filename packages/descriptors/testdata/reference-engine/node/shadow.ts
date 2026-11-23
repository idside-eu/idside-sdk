// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * The normalised shadow of a text, and the map back to the original.
 *
 * Rules never read the text itself but its shadow: each code point mapped on
 * its own — dropped when invisible, otherwise decomposed (NFKD), stripped of
 * combining marks, typographic dashes and quotes written as ASCII, anything
 * outside the Basic Multilingual Plane written U+FFFD. Every shadow character
 * is one UTF-16 unit, as it is one Python character, so offsets in the shadow
 * are the same in both SDKs; offsets in the original are this language's own.
 * The lowered shadow lowers A–Z only (`toLowerCase` and Python's `lower` do
 * not agree on every other letter). Python's twin is `_cleaning/shadow.py`.
 */

import { NORMALIZATION } from "./rules.ts";

function inRanges(ranges: readonly [number, number][], code: number): boolean {
  return ranges.some(([low, high]) => low <= code && code <= high);
}

const EQUIVALENTS = new Map<number, string>(NORMALIZATION.equivalents);
const CACHE = new Map<number, string>();

/** The shadow of one code point (possibly empty, possibly several characters). */
export function mapCodePoint(code: number): string {
  const cached = CACHE.get(code);
  if (cached !== undefined) return cached;
  let out = "";
  for (const char of String.fromCodePoint(code).normalize("NFKD")) {
    const point = char.codePointAt(0) as number;
    if (inRanges(NORMALIZATION.combining, point) || inRanges(NORMALIZATION.ignorable, point)) continue;
    const ascii = EQUIVALENTS.get(point);
    if (ascii !== undefined) out += ascii;
    else if (point > 0xffff) out += "\ufffd";
    else out += char;
  }
  CACHE.set(code, out);
  return out;
}

const ASCII = /^[\x00-\x7f]*$/;

function lowerAscii(text: string): string {
  return text.replace(/[A-Z]+/g, (run) => run.toLowerCase());
}

/**
 * A text's shadow, its ASCII-lowered twin, and the map back to the text.
 *
 * `segments` is undefined when offsets are shared (ASCII text). Otherwise each
 * entry is `[shadowStart, originalStart, originalLength, aligned]`: an aligned
 * run maps one shadow character to one original unit; a lone character that
 * maps to zero, two or more shadow characters, or that is two UTF-16 units,
 * is a segment of its own.
 */
export class Shadow {
  readonly text: string;
  readonly lower: string;
  private readonly segments: [number, number, number, boolean][] | undefined;

  constructor(text: string, lower: string, segments: [number, number, number, boolean][] | undefined) {
    this.text = text;
    this.lower = lower;
    this.segments = segments;
  }

  private segment(index: number): [number, number, number, boolean] {
    const segments = this.segments as [number, number, number, boolean][];
    let low = 0;
    let high = segments.length - 1;
    while (low < high) {
      const middle = (low + high + 1) >> 1;
      if (segments[middle][0] <= index) low = middle;
      else high = middle - 1;
    }
    return segments[low];
  }

  /** The original offset where shadow character `index` comes from. */
  start(index: number): number {
    if (this.segments === undefined) return index;
    const [start, origin, , aligned] = this.segment(index);
    return aligned ? origin + (index - start) : origin;
  }

  /** The original offset just past the character the shadow ends inside. */
  end(end: number): number {
    if (this.segments === undefined) return end;
    const last = end - 1;
    const [start, origin, length, aligned] = this.segment(last);
    return aligned ? origin + (last - start) + 1 : origin + length;
  }
}

const NON_ASCII = /[\ud800-\udbff][\udc00-\udfff]|[^\x00-\x7f]/g;

export function shadowOf(text: string): Shadow {
  if (ASCII.test(text)) return new Shadow(text, lowerAscii(text), undefined);
  const segments: [number, number, number, boolean][] = [];
  const pieces: string[] = [];
  let shadowAt = 0;
  let runStart = 0; // original offset where the current aligned run began
  let runShadow = 0;
  let position = 0; // original offset copied so far
  let odd = false;
  const closeRun = (until: number) => {
    if (until > runStart) segments.push([runShadow, runStart, until - runStart, true]);
  };
  for (const match of text.matchAll(NON_ASCII)) {
    const at = match.index;
    const char = match[0];
    if (at > position) {
      pieces.push(text.slice(position, at));
      shadowAt += at - position;
    }
    const mapped = mapCodePoint(char.codePointAt(0) as number);
    if (mapped.length === 1 && char.length === 1) {
      pieces.push(mapped); // one unit for one unit: stays in the aligned run
      shadowAt += 1;
    } else {
      odd = true;
      closeRun(at);
      if (mapped.length > 0) segments.push([shadowAt, at, char.length, false]);
      pieces.push(mapped);
      shadowAt += mapped.length;
      runStart = at + char.length;
      runShadow = shadowAt;
    }
    position = at + char.length;
  }
  if (position < text.length) pieces.push(text.slice(position));
  closeRun(text.length);
  const shadow = pieces.join("");
  return new Shadow(shadow, lowerAscii(shadow), odd ? segments : undefined);
}

/** The lowered shadow of `text`: how custom terms and domains are compared. */
export function fold(text: string): string {
  return shadowOf(text).lower;
}
