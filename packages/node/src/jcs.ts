// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Minimal RFC 8785 (JCS) canonicalization — internal module.
 *
 * The `request_hash` bound into ACT tokens is computed over the JCS canonical
 * form of the AI call payload, so every ID Side SDK (Python, Node, Ruby)
 * produces bit-identical hashes for the same payload and any auditor can
 * recompute them. The shared vectors in `packages/descriptors/testdata/` are
 * the cross-language contract.
 *
 * JavaScript happens to align with RFC 8785 for the pieces that usually differ:
 *   - `String(number)` is exactly ECMAScript `Number::toString` (§3.2.2.3),
 *   - `JSON.stringify(string)` produces the required escaping with raw UTF-8,
 *   - the default string sort orders by UTF-16 code units (§3.2.3).
 * So this module stays tiny and dependency-free.
 *
 * Scope: JSON-compatible values only (object / array / string / number /
 * boolean / null). Anything else must be normalized *before* calling
 * {@link canonicalize} (see the adapter's request-metadata extraction).
 */

/** Largest integer magnitude exactly representable as an IEEE-754 double (2^53). */
const MAX_SAFE_INT = 2 ** 53;

/** Matches a lone (unpaired) UTF-16 surrogate — a string that cannot encode to UTF-8. */
const LONE_SURROGATE = /\p{Surrogate}/u;

/** Reusable UTF-8 encoder (stateless) — allocating one per call would waste work on the hot path. */
const ENCODER = new TextEncoder();

/** Raised for any value outside the JCS-serializable domain. */
export class CanonicalizationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CanonicalizationError";
  }
}

/** Serialize `value` per RFC 8785 and return the canonical UTF-8 bytes. */
export function canonicalize(value: unknown): Uint8Array {
  return ENCODER.encode(serialize(value));
}

/** Serialize `value` per RFC 8785 and return the canonical string. */
export function canonicalizeToString(value: unknown): string {
  return serialize(value);
}

function serialize(value: unknown): string {
  if (value === null) return "null";

  const type = typeof value;
  if (type === "boolean") return value ? "true" : "false";
  if (type === "string") return encodeString(value as string);
  if (type === "number") return formatNumber(value as number);

  if (Array.isArray(value)) {
    let out = "[";
    for (let i = 0; i < value.length; i++) {
      if (i) out += ",";
      out += serialize(value[i]);
    }
    return out + "]";
  }

  if (type === "object") {
    // Reject exotic objects (Date, Map, class instances…) rather than letting
    // Object.keys silently mis-serialize them — same strictness as the Python SDK.
    const proto = Object.getPrototypeOf(value);
    if (proto !== Object.prototype && proto !== null) {
      throw new CanonicalizationError(
        `value of type '${(value as object).constructor?.name ?? "object"}' is not a plain JSON object`,
      );
    }
    const obj = value as Record<string, unknown>;
    // RFC 8785 §3.2.3: keys sort by UTF-16 code units, which is exactly what the
    // default Array#sort comparator does for strings.
    const keys = Object.keys(obj).sort();
    let out = "{";
    for (let i = 0; i < keys.length; i++) {
      if (i) out += ",";
      const key = keys[i] as string;
      out += encodeString(key) + ":" + serialize(obj[key]);
    }
    return out + "}";
  }

  // undefined, bigint, function, symbol
  throw new CanonicalizationError(`type '${type}' is not JSON-serializable`);
}

function encodeString(value: string): string {
  // Python rejects lone surrogates (UTF-8 encode failure); JSON.stringify would
  // instead emit a \uXXXX escape. Reject here so hashes cannot diverge across SDKs.
  if (LONE_SURROGATE.test(value)) {
    throw new CanonicalizationError("string contains a lone surrogate and is not encodable as UTF-8");
  }
  return JSON.stringify(value);
}

function formatNumber(value: number): string {
  if (!Number.isFinite(value)) {
    throw new CanonicalizationError("NaN and Infinity are not valid JSON numbers");
  }
  // ECMAScript Number::toString (RFC 8785 §3.2.2.3). String(-0) === "0" already.
  const text = String(value);
  // A number in plain (non-exponential) integer form beyond ±2^53 cannot be held
  // exactly by a JS double, and the Python SDK rejects such integers outright.
  // Reject here too, so the request_hash stays byte-identical across SDKs (fail
  // closed) rather than silently hashing a rounded value. Float magnitudes like
  // 1e30 print in exponential form and serialize identically everywhere, so they
  // pass — only the non-exponential integer form is refused.
  if (Number.isInteger(value) && Math.abs(value) > MAX_SAFE_INT && !text.includes("e")) {
    throw new CanonicalizationError(
      `integer ${text} exceeds the IEEE-754 safe range (±2^53); ` +
        "it would not hash identically across SDK languages",
    );
  }
  return text;
}
