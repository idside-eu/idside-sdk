/**
 * A stand-in cloud's answer for an application that cleans no prompt.
 *
 * Since prompt cleaning, `HttpConsentClient` asks `/v1/content-policy` before
 * every first authorize. A test written about authorize wraps its stub fetch
 * with this: the policy route answers `{ "enabled": false }` (cleaning off,
 * no `content_policy` field sent), every other request reaches the stub as
 * before. Python's suites do the same in their mock transports.
 */
export function cleaningOff(fetch: typeof globalThis.fetch): typeof globalThis.fetch {
  return (async (url: string | URL | Request, init?: RequestInit) => {
    const href = typeof url === "string" ? url : url instanceof URL ? url.href : url.url;
    if (new URL(href).pathname === "/v1/content-policy") {
      return new Response(JSON.stringify({ enabled: false }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return fetch(url, init);
  }) as typeof globalThis.fetch;
}
