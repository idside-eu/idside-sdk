// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Where a wrapped client sends its requests, reduced to what can be compared.
 *
 * The net used to bind an authorized request to its method and path, and never
 * to its host. A per-call `serverURL`, a derived client pointed elsewhere, a
 * `client.baseURL = …` after `wrap()`: each sent the prompt, the ACT token and
 * the provider key to an address nobody had wrapped, carrying a token that
 * matched. So `wrap()` freezes the client's origin — scheme, host and effective
 * port, and nothing else of the URL — and the net refuses a body-bearing
 * request bound anywhere else.
 *
 * One module for both halves on purpose, as `packages/python/src/idside/
 * _destination.py` is for the Python SDK: the origin frozen from a client's
 * base URL and the origin read off an outgoing request must be normalised the
 * same way, or the comparison refuses the client's own traffic. Two copies
 * agree on the day they are written and on no day after it.
 *
 * Nothing here reads a request's body, headers or query string, and nothing
 * here hashes, logs or sends anything: a URL goes in, and what comes out is a
 * host to compare or a path to name in a refusal.
 */

/**
 * Scheme, host and effective port, as the URL standard serializes them — or
 * undefined when the value names no server.
 *
 * So `HTTPS://API.OpenAI.com:443` and `https://api.openai.com` are one server,
 * and `http://` is another. Only a string or a `URL` is read: anything else
 * names no server rather than being coerced into one, because a value that has
 * to be stringified before it looks like a URL is not a destination this SDK
 * can hold a client to. An opaque origin (`data:`, `file:`) serializes as the
 * literal `"null"`, which is not an address either.
 */
export function originOf(value: unknown): string | undefined {
  if (typeof value !== "string" && !(value instanceof URL)) return undefined;
  try {
    const origin = new URL(String(value)).origin;
    return origin === "null" ? undefined : origin;
  } catch {
    return undefined;
  }
}

/** What the net calls an origin it could not read. It matches no bound origin. */
export const UNKNOWN_ORIGIN = "(unknown origin)";

/**
 * The same origin, for the half that has a message to write.
 *
 * The net compares origins and never shows them, but it does have to answer
 * with something for a URL it cannot read, and `undefined` compared against a
 * bound origin would read as "no binding" rather than "no address".
 */
export function requestOrigin(url: unknown): string {
  return originOf(url) ?? UNKNOWN_ORIGIN;
}

/** What the net calls a path it could not read. */
export const UNKNOWN_PATH = "(unknown path)";

/**
 * The URL's path, and only its path.
 *
 * The query string is dropped rather than trimmed for display: it is request
 * content, it can carry identifiers, and this goes into a refusal message that
 * ends up in somebody's logs. A URL that will not parse yields no detail at all
 * rather than a best-effort echo of a string that may hold credentials — which
 * is also why anything but a string or a `URL` yields nothing: `String(value)`
 * on an object nobody vouched for is a way of printing it.
 */
export function requestPath(url: unknown): string {
  if (typeof url !== "string" && !(url instanceof URL)) return UNKNOWN_PATH;
  try {
    return new URL(String(url)).pathname;
  } catch {
    return UNKNOWN_PATH;
  }
}
