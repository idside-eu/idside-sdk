// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * The prompt-cleaning catalogue, loaded once from the bundled rules file.
 *
 * `content-rules.json` is a byte-identical copy of
 * `packages/descriptors/content-rules.json`, shared with the Python SDK and
 * the ID Side cloud. Its SHA-256 is the `rules` digest every cleaned call
 * declares. The patterns in it are written in a subset both regex engines
 * read alike, so they are compiled here as they are: flags `g` and `d`, `i`
 * when the pattern asks for it, and never `u` (with `u`, `i` would let U+017F
 * match `s` where Python's `re.ASCII` does not).
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** The version of the engine in this package. */
export const ENGINE_VERSION = "1";

const here = dirname(fileURLToPath(import.meta.url));

/** The rules file as its bytes: what the `rules` digest is computed over. */
export const RULES_BYTES: Buffer = readFileSync(join(here, "..", "content-rules.json"));

interface RawPattern {
  regex: string;
  group: number;
  ignore_case?: boolean;
  keywords?: string[];
  prefilter?: string[];
}

interface RawDetector {
  id: string;
  label: string;
  validator?: string;
  patterns: RawPattern[];
}

interface RawRules {
  engine: string;
  normalization: {
    ignorable: [number, number][];
    combining: [number, number][];
    equivalents: [number, string][];
    word: [number, number][];
  };
  placeholder: { regex: string };
  limits: Record<string, number>;
  detectors: RawDetector[];
  tables: {
    iban_lengths: Record<string, number>;
    country_codes: string[];
    card_networks: { from: string; to: string; lengths: number[] }[];
  };
}

export const RULES: RawRules = JSON.parse(RULES_BYTES.toString("utf8")) as RawRules;
export const LIMITS: Record<string, number> = RULES.limits;
export const NORMALIZATION = RULES.normalization;
export const TABLES = RULES.tables;
export const PLACEHOLDER = new RegExp(RULES.placeholder.regex, "g");

/** Escape `text` so that both engines read it as literal characters. */
export function escapeRegExp(text: string): string {
  return text.replace(/[\\^$.*+?()[\]{}|/-]/g, "\\$&");
}

function classBody(ranges: [number, number][]): string {
  const hex = (code: number) => "\\u" + code.toString(16).padStart(4, "0");
  return ranges.map(([low, high]) => `${hex(low)}-${hex(high)}`).join("");
}

/** Letters and digits for whole-word custom terms, after normalisation. */
export const WORD_CLASS = classBody(NORMALIZATION.word);

/** One compiled pattern of a rule: the value is capture group `group`. */
export interface Pattern {
  readonly regex: RegExp;
  readonly group: number;
  readonly keywords: RegExp | undefined;
  /** Lowercase literals one of which must appear in the lowered shadow; `#digit`: any ASCII digit. */
  readonly prefilter: readonly string[];
}

export function keywordRegex(words: readonly string[]): RegExp | undefined {
  if (words.length === 0) return undefined;
  // Longest first, then alphabetical (the schema makes them unique), as Python sorts them.
  const sorted = [...words].sort((a, b) => b.length - a.length || (a < b ? -1 : 1));
  const alternatives = sorted.map((word) => word.split(" ").map(escapeRegExp).join("[ \\t\\r\\n]{1,4}")).join("|");
  return new RegExp("(?:^|[^a-z0-9])(" + alternatives + ")(?![a-z0-9])", "gd");
}

function compilePattern(raw: RawPattern): Pattern {
  const keywords = raw.keywords ?? [];
  const prefilter = raw.prefilter && raw.prefilter.length > 0 ? raw.prefilter : keywords;
  return {
    regex: new RegExp(raw.regex, raw.ignore_case ? "gdi" : "gd"),
    group: raw.group,
    keywords: keywordRegex(keywords),
    prefilter,
  };
}

export interface Detector {
  readonly id: string;
  readonly label: string;
  readonly index: number;
  readonly patterns: readonly Pattern[];
  readonly validator: string | undefined;
}

export const DETECTORS: ReadonlyMap<string, Detector> = new Map(
  RULES.detectors.map((raw, index) => [
    raw.id,
    { id: raw.id, label: raw.label, index, patterns: raw.patterns.map(compilePattern), validator: raw.validator },
  ]),
);

export const CATALOGUE_IDS: readonly string[] = RULES.detectors.map((raw) => raw.id);

/** Why this package's rules cannot be applied, or `undefined` when they can. */
export function catalogueProblem(): string | undefined {
  if (RULES.engine !== ENGINE_VERSION) {
    return `the bundled prompt-cleaning rules are for engine ${JSON.stringify(RULES.engine)}, this SDK runs engine "${ENGINE_VERSION}"`;
  }
  return undefined;
}
