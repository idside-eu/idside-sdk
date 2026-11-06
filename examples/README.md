# Examples

Three programs, in the two languages the SDK ships, that you can run right now:
clone, one setup command, watch a consent-enforced AI call happen and read what
actually left the process.

```
examples/
  python/  wrapped_call.py           a consent-enforced OpenAI call, end to end
           async_wrapped_call.py     the same, on an async client, two users at once
  node/    wrapped-call.ts           the same call in TypeScript, with no cast
  .env.example                       what the SDK reads from the environment
```

## What runs, and what does not

**What runs is the SDK.** Resolving the provider from its
[descriptor](../packages/descriptors/), patching the declared call sites,
canonicalising the request and hashing it, consuming the identity, applying the
parameters the decision forces, attaching the proof to the outgoing request,
deciding blocking-versus-async from the shape of the client you wrapped, and the
transport guard that refuses any body-bearing request that did not go through
enforcement. None of that is simulated.

**Two things are stand-ins, and the examples say which, out loud, in their own
output.**

- **The ID Side cloud.** `idside.eu` is not open to the public yet, so no
  account exists to authorize against and no ACT token can be signed — the
  signing keys live only in the cloud, never in this repository. Each example
  injects a `consentClient` / `consent_client=` that returns a decision of the
  right shape with an unsigned placeholder token. That injection point is the
  same one the test suites use.
- **The provider's endpoint.** `api.openai.com` is replaced by a stub
  transport, which is what lets these examples *print the outgoing request* —
  the headers and the body, exactly as they left. No provider key, no bill, no
  network. Every key and identifier in these files is fictitious.

**What therefore cannot be shown here:** a real signed proof, and so a real
`verify_act()` / `verifyAct()` round trip. An example that signed one would need
a private key, and this repository holds none by design — verification is
checked instead against fixed cross-language vectors in both test suites.

**The packages are not published yet** (neither on PyPI nor on npm), so each
example project resolves the SDK from this clone. It is one line in
`python/pyproject.toml` and one in `node/package.json`, both marked; deleting
them is the entire migration once the packages ship.

## Python

Needs Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
cd examples/python
uv sync
```

### A consent-enforced call, end to end

```console
$ uv run wrapped_call.py
1. The call the developer writes
   client.chat.completions.create(
       model='gpt-4o-mini',
       messages=[{'role': 'user', 'content': 'Summarize my last consultation.'}],
       user_id='usr_marie_456',
   )

2. What ID Side was asked
   provider=openai model=gpt-4o-mini call_site=chat.completions.create
   request_hash=sha256:b3accbf010c27c0d63129c7056f372a02579d48e60825f944ed43b5f1f33b311
   user_id='usr_marie_456' ubt=None
   credential_postures=[]
   ...and nothing else. The prompt is not in this request.

3. What ID Side answered
   consent_state={'training': False, 'retention.conversation': False}
   params_forced={'store': False}   <- the SDK applies it, not you

4. What actually left this process
   POST https://api.openai.com/v1/chat/completions
   X-AI-Consent-Token: act.eyJhbGciOiJFUzI1NiJ9.fake.signature
   body: {"messages":[{"role":"user","content":"Summarize my last consultation."}],"model":"gpt-4o-mini","store":false}

5. What the provider replied
   'ok'

Three things to read twice:
   - the cloud was given a hash, never the prompt;
   - store=false — ID Side's decision — reached OpenAI's request body;
   - user_id did NOT. The SDK consumed it; the provider never sees it.
```

### The same enforcement, async, for two users at once

`wrap()` asks the client what shape it is — an `AsyncOpenAI` answers by having
coroutine call sites — and that one answer decides both which wrapper each call
site gets and whether the hop to the cloud blocks. This example does not assert
that the hops overlap; it measures them.

```console
$ uv run async_wrapped_call.py
1. wrap() saw an AsyncOpenAI, and chose the async path for everything
   the call sites are awaited, and so is the hop to ID Side

2. Two users, two calls, one event loop
   usr_marie_456  request_hash=sha256:b3accbf010c27c0d63129c7056f372a02579d48e60825f944ed43b5f1f33b311
   usr_paul_789   request_hash=sha256:98ff5d05b5a47fb1ab8bfacc474f58abf65c1b9f92ba44846fd42eca0da457d5

3. The two authorize hops overlapped: True
   a blocking client would have made them queue, one round trip each

4. What left this process (sorted, because the loop decides the order)
   {"messages":[{"role":"user","content":"Summarize my last consultation."}],"model":"gpt-4o-mini","store":false}
   {"messages":[{"role":"user","content":"When is my next appointment?"}],"model":"gpt-4o-mini","store":false}

   Both carry store=false, which ID Side decided and the SDK applied.
   Neither carries a user_id: it never reaches the provider, so these
   two requests cannot be told apart here except by what was asked.
```

## Node.js

Needs Node 23.6+ (the examples are TypeScript run in place) and npm. The SDK is
not on npm yet and npm does not build a `file:` dependency, so build it first:

```bash
npm --prefix packages/node run build
cd examples/node
npm install
```

### A consent-enforced call, typed

The identity is part of the type: this file compiles under `strict` with no cast
anywhere, and dropping `user_id` from the call would not compile at all. Running
a `.ts` file only strips the types, so the check that matters is separate:

```bash
npm run typecheck
```

```console
$ node wrapped-call.ts
1. The call the developer writes
   await client.chat.completions.create({
     model: "gpt-4o-mini",
     messages: [{ role: "user", content: "Summarize my last consultation." }],
     user_id: "usr_marie_456",
   });

2. What ID Side was asked
   provider=openai model=gpt-4o-mini callSite=chat.completions.create
   requestHash=sha256:b3accbf010c27c0d63129c7056f372a02579d48e60825f944ed43b5f1f33b311
   userId=usr_marie_456 ubt=undefined
   credentialPostures=[]
   ...and nothing else. The prompt is not in this request.

3. What ID Side answered
   consentState={ training: false, 'retention.conversation': false }
   paramsForced={ store: false }   <- the SDK applies it, not you

4. What actually left this process
   POST https://api.openai.com/v1/chat/completions
   X-AI-Consent-Token: act.eyJhbGciOiJFUzI1NiJ9.fake.signature
   body: {"model":"gpt-4o-mini","messages":[{"role":"user","content":"Summarize my last consultation."}],"store":false}

5. What the provider replied
   "ok"

Three things to read twice:
   - the cloud was given a hash, never the prompt;
   - store=false — ID Side's decision — reached OpenAI's request body;
   - user_id did NOT. The SDK consumed it; the provider never sees it.
```

## One payload, one hash, two languages

The Python and Node examples send the same model and the same prompt, and print
the same `request_hash`:

```
sha256:b3accbf010c27c0d63129c7056f372a02579d48e60825f944ed43b5f1f33b311
```

The request *bodies* they print are ordered differently, because each provider
SDK serialises its own way. The hash is not, because ID Side canonicalises the
request ([JCS, RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)) before hashing
it. That is what lets an auditor recompute the hash from your request, in
whatever language they like, and check it against the proof.

## Going live

Two lines per example, once you have an ID Side account and a provider key:

```diff
-        OpenAI(
-            api_key="sk-test-not-a-real-key",
-            http_client=httpx.Client(transport=httpx.MockTransport(openai_stub)),
-        ),
-        consent_client=FakeCloud(),
+        OpenAI(),                    # reads OPENAI_API_KEY
+                                     # and IDSIDE_API_KEY for the cloud
```

Everything else in the file — the call, the identity, the error handling — is
already what you would ship. The environment variables are in
[`.env.example`](./.env.example); the [Python](../docs/quickstart-python.md) and
[Node](../docs/quickstart-node.md) quickstarts cover the rest, including the
errors worth catching and how to read the journal.

## Keeping these honest

An example nothing runs rots exactly like the documentation around it, so CI
runs all three on every pull request and diffs their output against the
`console` blocks above, character for character —
`scripts/check_docs_examples.sh`, the same script that does it for the
quickstarts. A new example is a file, a `console` block here, and one line in
that script.
