# Prompt cleaning

> **Status.** Unreleased: this page describes what the next release of both
> SDKs does. The wire fields it names (`/v1/content-policy`, the
> `content_policy` request and response fields) are additions to `/v1`; the
> claim a certificate may carry about cleaning is subject to change until released.

An organization can decide, on the ID Side dashboard, what must not leave its
applications in a prompt: e-mail addresses, phone numbers, payment cards, IBANs,
French identifiers, provider API keys and tokens, private keys, credentials, and
its own terms, domains and reference patterns. For each, it chooses **mask** (the
value is replaced, the call goes out) or **block** (the call does not go out).

The SDK applies that policy **inside your process, before the call leaves**.
The prompt never reaches ID Side: the cloud receives the policy's digest, the
engine's digests and, for each rule, the *number* of values masked — never a
value, never an excerpt.

Cleaning **reduces the risk** that such data reaches an AI provider. It does
not detect everything: it reads text with fixed patterns and check digits, it
cannot read images or files, and someone determined to get a value past it can
(see [what it does not catch](#what-it-does-not-catch)). It supports an
organization's own controls and helps it show what it applied; it does not
promise an outcome.

## What a call goes through

```text
your call → options checked → cleaned → hashed → /v1/authorize → provider
                                 │
                                 └─ block: ContentBlockedError, nothing sent
```

1. On the first call, and again after the time the cloud gives, the SDK fetches
   the strainer's policy from `GET /v1/content-policy` — whether or not the
   organization cleans anything: the answer is what says so. A policy is signed
   by ID Side (ES256, `typ: content-policy+jwt`) under a key of its own: the
   cloud's key set (`/.well-known/jwks.json`, fetched without your API key)
   holds one key per purpose, named after it (`content-policy-g1`, `act-g1`),
   and a policy signed with any key but a `content-policy-` one is refused, so
   the key that signs certificates cannot sign what your calls are cleaned by. The SDK
   then checks the signature, the type, issuer and expiry, that the policy is
   the one its digest names, and that it was issued for the strainer it
   names; then that its dates hold by this machine's clock (a refusal for that
   reason says to check the clock, and, for a policy already expired on
   arrival, that no cache between this machine and the cloud served an old
   answer). A policy that does not check out is never
   applied, and the call is not made (`ContentPolicyError`). In Python the signature is checked by
   `cryptography` when the `[verify]` extra is installed, and by verification
   code built into the package otherwise: a bare `pip install idside` checks it
   too.
2. The call's body is cleaned. A value a *mask* rule finds is replaced by a
   placeholder; a value a *block* rule finds stops the call.
3. The request hash is computed on the cleaned body — **the hash is always of
   what leaves** — with the same function as before
   ([spec §3.1](./spec/act-token.md#31-request_hash--canonicalization)).
4. `/v1/authorize` is told what was applied (the `content_policy` field below).

**A strainer with nothing enabled sends what it sent before:** the cloud
answers `{"enabled": false}`, nothing is cleaned, the authorize request carries
no `content_policy` field, and the request hash is the one an SDK without
cleaning computes.

**What the provider library is handed** is the arguments you passed, with the
masked strings written into a copy of each argument that held one: an argument
with nothing to mask is your own object, and a typed one stays typed (a reply
object passed back into `messages`, a Pydantic model, a config naming a class).
What cannot be written into leaves as its cleaned JSON: an object that writes
itself with `toJSON()` in Node, and, in Python, a class passed where a schema is
expected (`response_format=Contact`, Gemini's `response_schema=Contact`) when a
description in that schema held something masked. That class then leaves as its
plain JSON schema, and the library no longer reads the answer into it: keep
values a rule would mask out of a schema's descriptions to keep the class.

No `wrap()` option and no per-call option turns cleaning off on a client
connected to the ID Side cloud: the policy is the organization's, set where your
code cannot change it, like the rest of what applies to a call. A
`consent_client=` / `consentClient` of your own replaces that cloud entirely: it
is meant for tests only, and its calls carry no cleaning certificate — those that
still reach the cloud are counted as calls with no cleaning declared.

## Placeholders

A masked value becomes `[LABEL_n]`: `[EMAIL_1]`, `[CARD_1]`, `[API_KEY_2]`. A
value is replaced whole: one that runs on (a token, a password to its
delimiter, a key cut before its END line to the end of the text) is followed to
its end, and two findings that overlap become one placeholder, named by the
longest. The
same value gets the same placeholder everywhere in one call — in the system
prompt, the messages, a tool call's arguments — so the model can still follow
who is who. Numbering starts above any placeholder already in the call, so a
conversation that carries earlier placeholders keeps them distinct, and a
placeholder is never itself matched by any rule.

## What is read

Every string of the call's body is read — system prompts, messages, tool
definitions, tool calls and tool results, metadata — with three exceptions,
declared per provider in its [descriptor](../packages/descriptors/README.md):

- **identifiers and settings** (`model`, `role`, `type`, ids, tool names…) are
  never read nor changed: changing one would break the call;
- **attachments** — images, audio, documents and files sent by URL, id or as
  base64 — are never read (their content is not text); they are counted, and a
  policy may block a call carrying one;
- **object keys** are never read: only values are.

Where you name the keys — OpenAI's prompt `variables`, metadata, Gemini's
`labels`, a tool call's arguments, the properties, definitions and dependent
schemas of a JSON Schema (a tool's parameters, a structured output) — a key is
a name, not an identifier: what a key called `name`, `id` or `type` holds is
read like any other value, and by the usual rules — an image passed as a prompt
variable is still an attachment, and the `type` of a schema property is still
not text. What a schema holds for you — `default`, `const`, `examples`, `enum`
— is data, read whole whatever its keys.

Text is compared after normalisation: invisible characters are dropped,
full-width forms and ligatures are decomposed, accents and typographic dashes
and quotes are set aside. `ｍａｒｉｅ＠example.com` and `andré@example.com` are
found as addresses, and replaced whole. An escape written in two characters
(`\n`, `\t`, `\r`, as in JSON or a tool result pasted as text) stands where a
line break would: a value right after it is read as after a real one, and the
letter of the escape is not taken with it. That normalisation follows the Unicode
tables of the runtime it runs on (Python's `unicodedata`, the ICU data Node.js
ships): a character added to Unicode after version 13 may be decomposed by one
runtime and left as it is by another.

## The rules

The catalogue lives in one file, [`content-rules.json`](../packages/descriptors/content-rules.json),
bundled byte-identical in both SDKs. Each rule is an entry of its `detectors`
list, and its id is what a policy names and what counts are reported under.

| Group | Rules | How a value is confirmed |
| --- | --- | --- |
| Contact details | `email`, `phone`, `ip_address`, `mac_address` | format; IP addresses parsed |
| Banking | `credit_card`, `iban`, `bic` | Luhn and card network; mod 97 and country length; BIC country, next to the word BIC or SWIFT |
| French identifiers | `fr_nir`, `fr_siren`, `fr_siret`, `fr_vat`, `fr_plate` | NIR key (Corsica included); Luhn (La Poste rule included), next to SIREN / SIRET; VAT key |
| AI provider keys | `key_openai`, `key_anthropic`, `key_google`, `key_mistral`, `key_deepseek`, `key_idside` | prefix and length; a Mistral key next to the word Mistral |
| Technical keys | `key_aws`, `key_github`, `key_gitlab`, `key_slack`, `key_stripe`, `key_twilio`, `key_sendgrid`, `key_npm`, `key_pypi`, `key_azure`, `jwt`, `bearer_token` | prefix and shape |
| Secrets | `private_key`, `connection_string_password`, `url_credentials` | format |
| Generic secrets | `generic_secret` (`password=…`, `"api_key": "…"`) | a name that says secret; off by default — it also finds values that are not secrets |

Each rule's `description` in the catalogue says what it reads, in one
sentence, and the forms it leaves aside.

An organization can add its own entries: **terms** (case and accents ignored,
whole words), **domains** (the domain and its subdomains, in e-mail addresses,
URLs and host names) and **patterns** (`#` a digit, `@` a letter, `*` a run of
letters and digits, anything else literal: `PRJ-####`). There are no free
regular expressions: a badly written one could stall every call of an
application, and two languages do not read one alike.

## What it does not catch

Said plainly, because a control is only useful when its limits are known. The
shared test vectors record most of these as cases the engine does not catch:
what it does not read is left as it is, and is not counted — a key cut across
two text values, below, is the one exception. What it counts, it
masks whole: a test in both SDKs puts every value the vectors name in generated
contexts (prefixes, suffixes, escaped newlines, values lengthened past every
bound, a private key cut before its END line) and holds that no run of four
characters of a counted value is left — and that in neutral contexts (a space,
punctuation, quotes, brackets, a line break real or escaped) it is counted at
all. Both suites also clean a generated corpus of over 700,000 texts with an
earlier version of the engine kept frozen in the repository, and fail if the
engine masks less than it did, except where a named exclusion says why
(`testdata/content-oracle.json`).

- a value written out or disguised: `marie at example dot com`, `marie [at]
  example.com`, an `@` between spaces, text in base64 or URL-encoded (`%40`), a
  number broken across two lines or two messages;
- an address in a script other than Latin, Greek or Cyrillic (`用户@例子.中国`),
  or with a local part longer than 64 characters;
- names of people and postal addresses: no pattern can find them reliably;
- what is inside an image, a PDF or any attachment, and what a URL points to;
- identifiers with no check digit and no context: a SIREN, SIRET or BIC without
  its word nearby, a 32-character key without the word Mistral or Azure;
- numbers grouped other than the usual way: a card in groups of 4-8-4, a French
  RIB written as bank, branch, account and key (even after `FR76`), French
  numbers or cards with mixed separators (`06 12-34.56 78`, `4111 1111-1111.1111`),
  a NIR whose key is written after a word (`… clé 42`), freephone numbers written
  `0 800 123 456`, an international number of fewer than 9 digits (`+500
  12345`);
- a MAC address written without separators (`001A2B3C4D5E`), with spaces, in
  two groups of six (`001a2b-3c4d5e`) or in the shortened form
  `0:1a:2b:3c:4d:5e`, and an IP address written with dashes
  in a host name (`ip-10-0-1-23`);
- four numbers right after `release` (an environment named release, too), and
  an IPv6 address written without a single digit (`dead:beef::cafe`);
- the older French plate format, and lower-case plates and BICs;
- `::1` and other compressed IPv6 addresses with no group of three hex digits
  or more: `items[::2]` is a slice of code as often as an address;
- an AWS secret access key (only the access key id has a prefix), a legacy
  GitLab runner registration token, any token whose format has no documented
  prefix;
- a private key cut before its END line when 32 base64 characters of key
  material do not come right after its BEGIN line: a word of prose between
  them, fewer than 32 of them left (a short key cut in its middle), or a marker
  that is not a placeholder (`[REDACTED]`, `...`, `<redacted>`) in place of
  part of them — the start of the body, or its first line from any of its
  first 32 characters (a marker further down leaves the key masked). The BEGIN
  line is then read as quoted: under Mask the key is left as it is, all of it,
  and not counted; under Block the call is blocked;
- a private key cut across two text values or two messages: only the value
  that holds its BEGIN line is masked, and the rest leaves as it is — for an
  RSA key, enough to rebuild it — while the key is counted as masked; under
  Block the call is blocked;
- a complete private key whose lines hold a placeholder, left by an earlier
  cleaning or typed (`Comment: [EMAIL_1]`): under Mask it leaves whole and is
  not counted; under Block the call is blocked;
- a password assigned to a name the catalogue does not list, a name written in
  camelCase (`dbPass`) included, unless `generic_secret` is on; a lookup of the
  environment (`os.environ[…]`, `os.getenv("…")`, `process.env.…`) or a
  reference to a variable in braces (`${NAME}`), in an assignment or as the
  password of a URL (`postgres://app:${DB_PASSWORD}@db`), is a name, not a
  password, and is left as it is, and so is the default such a
  lookup gives under a capitalised name ending `_KEY` (`SIGNING_KEY =
  os.getenv("SIGNING_KEY", "…")`), even with `generic_secret` on; the user or
  password of a URL whose first 256 characters hold neither an `@` nor its end;
  a password glued after an e-mail address and a colon when it holds a `/` (it
  reads as a path: `git@github.com:acme/app.git`);
- anything sent on a client that is not wrapped.

## What may be taken wrongly

A rule reads a shape and, where one exists, a check digit or a word nearby;
it does not know what a number means. These are masked (or block, under Block)
although they may not be what the rule names:

- a 10-digit number starting `01` to `09` (an order or lot number, an ISBN-10)
  is read as a French phone number; a number in `+33`, and any international
  number of 9 to 15 digits, stays a phone number whatever follows it, a
  currency included — an amount after a plus sign (`+123 456 789 012 €`) is
  masked as one, round thousands (`+150 000 000`) aside; letters or digits
  glued to a number are masked with it (`+44 20 7946 0958 9am` masks `9am`
  too), and when more groups of digits follow than a number holds, the longest
  reading of 15 digits or fewer is masked, which may take a year with it;
- four numbers of 0 to 255 (`1.2.3.4`) are read as an IPv4 address unless
  `version`, `ver.`, `release`, `firmware` or `revision` stands right before
  them, `v` is glued to them (`v1.2.3.4`) or stands before them with a space
  (`v 3.10.1.9`), or a fifth number follows: after `ver` without its dot,
  `build`, `fw`, `rev` or an option `-v`, they are taken; a slash and digits
  after an address go with it (`http://192.168.1.10/2024/report` masks
  `/2024`), and colon-separated hexadecimal groups glued to letters or digits
  are masked with them;
- about one in ten 16-digit numbers passes the Luhn check, so some delivery
  numbers and EAN barcodes are read as cards;
- an 8-letter word in capitals near the word SWIFT or BIC (`BUSINESS`) is read
  as a BIC; a 9-digit number passing Luhn near RCS as a SIREN; a few IMEIs as a
  NIR;
- a product code shaped like a plate (`EV-300-XL`) is read as a plate;
- dotted references `xxxx.xxxx.xxxx` in hexadecimal are read as MAC addresses;
- any 32 hexadecimal characters (an MD5 hash) next to the word Mistral or Azure
  are read as a key;
- everything between a private key's BEGIN line and the END line after it is
  masked, even prose; and when the END line is missing and key material
  follows, everything to the end of the text value is masked — too much, never
  half;
- an e-mail address followed by `:` and a port is taken as an address, the
  port left; a run glued after the colon that is not a port and holds no `/`
  is read as a password (`marie@example.com:S3cret`) and masked with the
  address, counted as one address — `user@host:repo.git` included; a git remote
  is an address, its path left when it holds a `/`, unless `git clone`,
  `git remote add` or `set-url`, `scp` or `rsync` stands right before it, and
  then nothing is taken; a dash, a digit or a label glued after an address's
  domain is masked with it;
- a key, a token or a password glued to more of the characters it is made of is
  masked with them, however long the run, URL escapes (`%2F`) included: a
  token followed by an escaped separator (`%26next=1`) takes what follows it;
- a BEGIN line followed right after it — nothing between them but blanks, line
  prefixes, quotes and PEM or PGP header lines — by 32 letters, digits, `+` or
  `/` in one or two runs, the first of 16 or more, reads as a key cut before
  its END line: everything after it in that text is masked, and counted as a
  private key. Tool output does this: a commit hash, a fingerprint, a UUID
  without dashes, a long path or identifier right after a BEGIN line (the
  output of `grep` or of a secret scanner, a log, a table, code that builds a
  PEM), and so does such a run in the value of a header line (`Comment:`
  followed by a fingerprint). A BEGIN line quoted in prose is left as it is
  under Mask, and not counted; under Block it blocks the call;
- a guide's placeholder after Bearer (`Bearer YOUR_ACCESS_TOKEN`) is masked as
  a token: a placeholder written in capitals cannot be told from a token;
- two findings that overlap are masked as one, under the longest: an address
  inside a longer finding goes with it;
- a password value that starts with `$`, `$NAME` without braces and a default
  (`${NAME:-x}`) included, is taken: patterns ignore case, so a variable name
  cannot be told from a password;
- a Stripe secret key with exactly 32 characters after `sk_live_` has the ID
  Side key format: it is masked, and counted as an ID Side key;
- `generic_secret`, off by default, also takes values that are not secrets
  (code, numbers, dates) assigned to a name that says password, secret or
  token.

Cleaning runs where your code runs. A developer who rewrites the SDK can send
the text untouched while declaring the policy applied; the certificate then states,
signed and dated, a policy applied that was not — which the replay check below
exposes to anyone holding the request.

The answer that says nothing is enabled (`{"enabled": false}`) is not signed:
whoever controls the network between the SDK and the cloud can make the SDK
believe nothing is enabled. The call then goes out without a declaration,
exactly as from an earlier SDK, and the cloud counts it as such.

## What the cloud is told

On every cleaned call, `/v1/authorize` receives an optional field:

```json
"content_policy": {
  "digest": "sha256:…",
  "engine": "1",
  "rules": "sha256:…",
  "build": "sha256:…",
  "challenge": { "token": "…", "response": "sha256:…" },
  "counts": { "email": 2, "custom:PROJECT": 1 }
}
```

- `digest`: the policy applied to this call (SHA-256 of its RFC 8785 form —
  `content_policy_digest()` / `contentPolicyDigest()`);
- `engine`, `rules`, `build`: the engine version, the digest of the rules file,
  and the digest of the SDK's cleaning code and descriptors as installed. These
  are what the SDK says about itself: the cloud lists them in the certificate when it
  recognises a published release, and an install it does not recognise — an
  older release, a vendored or rebuilt copy — is simply not listed, never
  refused;
- `challenge`: texts the cloud sent with the policy, cleaned by this SDK and
  hashed; an engine that does not clean them as the published one does gives a
  wrong answer, which the cloud records as an anomaly. A challenge stands as
  long as the policy it came with (fifteen minutes, and a minute more), and
  does not prevent the replay of an answer within that window; after it, and
  until an hour after the policy expired, a right answer only makes the call
  `outdated`, and later it is an anomaly;
- `counts`: how many values each rule masked in this call.

A blocked call never reaches `/v1/authorize`. The SDK reports it to
`POST /v1/content-events`, best effort, and raises
[`ContentBlockedError`](./errors.md#contentblockederror). The report carries:

- `digest` and `engine`: the policy that blocked the call, and the engine
  that applied it;
- `blocked`: each rule that blocked it, and how many times it matched — rule
  ids and counts, never what matched;
- `provider`: the provider the call was for, as its descriptor names it
  (`openai`);
- `model`: the model the call asked for, as the caller wrote it.

`provider` and `model` are left out — never sent empty or null — when they are
not of the shape the service keeps: a provider name of lowercase letters,
digits, `_` and `-` (at most 64), a model passed as a string of 1 to 200
printable ASCII characters with no blank at either end. A model the service
could not read would make it refuse the whole report; left out, the blocked
call is still counted, under no model. The report holds no identity and no
part of the prompt.

A report the cloud keeps — one under a revision of the application's policy,
for rules that revision sets to *block* — is recorded in the organization's
ledger as a blocked call, with its own certificate number (`evt_…`, which
`GET /v1/verify/{number}` answers with `status: "blocked"`). That entry is the
SDK's declaration and says so: the cloud never sees the prompt, so it records
what the report carries and nothing more. The report is sent best effort; one
lost on the way leaves no entry, and the call is blocked all the same.

The answer may carry `content_policy: {"status", "digest"}`. `applied` means
the declaration checked out; `outdated` means a newer policy exists, or that
the challenge was answered right more than a minute after the policy expired,
and within the hour after it expired (a clock behind, an answer served again by
a cache) — either way the SDK fetches the policy at once; `anomaly` means
something did not check out, a right answer more than an hour late included;
`not_applied` means the call
declared nothing. The answer never changes the call in
progress. Releases before this one send no `content_policy` field: their calls
are authorized as before, uncleaned, and the dashboard shows them as such.

## Replaying a request

A request the SDK sent is a **fixed point** of the policy it applied: cleaning
it again under that policy masks nothing and blocks nothing. Whoever holds the
request that left — the organization, an auditor — and the policy its certificate
names can check it, offline, years later, without ID Side:

```python
from idside import content_policy_digest, verify_clean

assert content_policy_digest(policy) == digest_named_by_the_certificate
verify_clean(request_body, policy, provider="openai")  # True: nothing left to do
```

```ts
import { contentPolicyDigest, verifyClean } from "@idside/sdk";

verifyClean(requestBody, policy, { provider: "openai" }); // true
```

Or from a shell, with either package installed:

```console
$ idside-verify-clean request.json --policy policy.json --provider openai --digest sha256:…
```

It exits 0 when the request is a fixed point, 1 when the policy would still
mask or block something (and says which rules, never which values), 2 when the
input cannot be read, the policy is not the one the digest names, or an error
stopped the check before it answered: 2 always means nothing was checked, never
"not clean". `True` means the policy, as this engine reads it, finds nothing
more to do — not that the request held nothing sensitive.

Normalisation follows the runtime's Unicode tables (see
[What is read](#what-is-read)): a request holding characters added to Unicode
after version 13 may replay differently on another runtime, or another version
of Python or Node.js, than the one that cleaned it. Replay such a request where
it was cleaned.

## Errors

| Error | When |
| --- | --- |
| [`ContentBlockedError`](./errors.md#contentblockederror) | a *block* rule matched; nothing was sent |
| [`ContentPolicyError`](./errors.md#contentpolicyerror) | the policy did not check out, or cannot be applied — or this machine's clock is out of step with it, or a cache served an old one, which the message says (set the clock: NTP) |
| [`ConsentUnreachableError`](./errors.md#consentunreachableerror) | the policy could not be fetched, and none still valid is held; the message names the HTTP status when the cloud answered one |
| [`RateLimitedError`](./errors.md#ratelimitederror) | the policy route answered 429, and again after the wait its `Retry-After` asked for (two seconds at most), while no valid policy is held |

## Cost

Measured on one core, median, on a prompt of French and English prose with a
few addresses, every rule enabled:

| Body | Python | Node.js |
| --- | --- | --- |
| 10 KB | ≈ 3.5 ms | ≈ 0.5 ms |
| 25 KB | ≈ 9 ms | ≈ 1 ms |
| 1 MB | ≈ 350 ms | ≈ 45 ms |

In Python, count about half a second per megabyte of prompt with every
rule enabled, more when a text is dense with values to mask (each costs a
few microseconds); Node.js is several times faster. The time grows in step with
the text, never faster.

Matching is linear in the size of the text — every pattern is bounded, a value
that runs on is followed to its end by the engine itself, each character read
once, what must follow a private key's BEGIN line (header lines, separators,
key material) is read by the engine in one pass, overlapping findings are
grouped in one sort, and each SDK's tests time adversarial inputs, a megabyte of each against
every rule alone, and a value of every such rule filling 1 MiB, 5 MiB and the
whole limit, to check it. A body of more than
8,388,608 characters of text is blocked rather than skipped. The policy is
fetched once and cached; no dependency is added to either package.

Because the SDK is fail-closed, a cloud that cannot serve
`GET /v1/content-policy` — unreachable, or answering an error — stops the call
with `ConsentUnreachableError` once no policy it already holds is still valid
(a signed policy is valid fifteen minutes), as an unreachable `/v1/authorize`
does. While one is held, a failed refresh is not retried on every call: the
cloud is asked again at most once every ten seconds, and the policy held is
applied in between. Once none is held, every call asks again.

However many calls need the policy at once — a process starting under load — a
wrapped client has one fetch of it, and of the key set, in flight: the other
calls wait for that one and share what it gives, a failure included. A failure
is not kept: the next call asks again. A 429 on that route while no valid
policy is held is retried once, after the `Retry-After` the cloud sent (in
seconds, two at most; without one, not at all), before the call stops with
`RateLimitedError`.

A policy held stops at the earlier of its signed expiry (`exp`) read on this
machine's clock, and its lifetime (`exp - iat`) counted from the moment it was
received on a clock no one can set. A machine whose clock runs ahead stops it
early; one whose clock runs behind keeps it past its real expiry by no more than
the age the answer had when it arrived (the time the cloud kept it ready, or a
cache in the way held it). An answer already old on arrival on a machine whose
clock also runs behind is the one case where that age adds to the delay: the
cloud may then refuse the call's cleaning as out of date.
