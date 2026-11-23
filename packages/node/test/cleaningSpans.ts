// Which characters of a text an engine masks: the oracle's measure.
//
// `masked()` replays an engine's own passes (its `find`, `resolveCandidates`
// and shadow) and follows each character of the original text through them,
// placeholders included, so that what a later pass masks is traced back to the
// original. The text it rebuilds is checked against the engine's own `clean()`
// by the oracle test: the replay is the engine, or the test fails. Written the
// same way in the Python suite (`tests/_cleaning_spans.py`).

/** What the replay needs of an engine: the modules of `cleaning/`. */
export interface EngineModules {
  readonly resolveCandidates: (candidates: readonly Candidate[], shadow: ShadowLike) => Candidate[];
  readonly shadowOf: (text: string) => ShadowLike;
  readonly PLACEHOLDER: RegExp;
  readonly LIMITS: Record<string, number>;
}

interface Candidate {
  readonly start: number;
  readonly end: number;
  readonly rule: { readonly label: string };
}

interface ShadowLike {
  readonly text: string;
  readonly lower: string;
  start(index: number): number;
  end(index: number): number;
}

type Finder = (shadow: ShadowLike, placeholders: { overlaps(start: number, end: number): boolean }) => [Candidate[], Candidate[]];

class Spans {
  private readonly items: [number, number][] = [];
  add(start: number, end: number): void {
    this.items.push([start, end]);
  }
  overlaps(start: number, end: number): boolean {
    return this.items.some(([low, high]) => low < end && start < high);
  }
}

/** [one flag per original character, 1 where masked; the cleaned text]; `undefined` flags when blocked. */
export function masked(engine: EngineModules, cleaner: object, text: string): [Uint8Array | undefined, string] {
  const find = (cleaner as { find: Finder }).find.bind(cleaner);
  const placeholder = new RegExp(engine.PLACEHOLDER.source, "g");
  const floors = new Map<string, number>();
  for (const match of engine.shadowOf(text).text.matchAll(placeholder)) {
    const inner = match[0].slice(1, -1);
    const cut = inner.lastIndexOf("_");
    const label = inner.slice(0, cut);
    floors.set(label, Math.max(floors.get(label) ?? 0, Number(inner.slice(cut + 1))));
  }
  const assigned = new Map<string, string>();
  const following = new Map<string, number>();
  const hidden = new Uint8Array(text.length);
  // For each character of the current text, the original interval it stands for.
  let low = Array.from({ length: text.length }, (_, index) => index);
  let high = Array.from({ length: text.length }, (_, index) => index + 1);
  let current = text;
  for (let round = 0; round < engine.LIMITS.max_passes; round++) {
    const shadow = engine.shadowOf(current);
    const holders = new Spans();
    for (const match of shadow.text.matchAll(placeholder)) holders.add(match.index, match.index + match[0].length);
    const [masks, blocks] = find(shadow, holders);
    if (engine.resolveCandidates(blocks, shadow).length > 0) return [undefined, text];
    if (masks.length === 0) return [hidden, current];
    const pieces: string[] = [];
    const newLow: number[] = [];
    const newHigh: number[] = [];
    let position = 0;
    for (const candidate of engine.resolveCandidates(masks, shadow)) {
      const start = shadow.start(candidate.start);
      const end = shadow.end(candidate.end);
      const label = candidate.rule.label;
      const value = label + "\u0000" + shadow.lower.slice(candidate.start, candidate.end);
      if (!assigned.has(value)) {
        const number = following.get(label) ?? (floors.get(label) ?? 0) + 1;
        following.set(label, number + 1);
        assigned.set(value, `[${label}_${number}]`);
      }
      const stands = assigned.get(value) as string;
      let first = Infinity;
      let last = -Infinity;
      for (let index = start; index < end; index++) {
        first = Math.min(first, low[index]);
        last = Math.max(last, high[index]);
      }
      hidden.fill(1, first, last);
      pieces.push(current.slice(position, start), stands);
      for (let index = position; index < start; index++) {
        newLow.push(low[index]);
        newHigh.push(high[index]);
      }
      for (let index = 0; index < stands.length; index++) {
        newLow.push(first);
        newHigh.push(last);
      }
      position = end;
    }
    pieces.push(current.slice(position));
    for (let index = position; index < current.length; index++) {
      newLow.push(low[index]);
      newHigh.push(high[index]);
    }
    current = pieces.join("");
    low = newLow;
    high = newHigh;
  }
  return [undefined, text];
}
