# Preference Center widget

The widget is the half of ID Side your users see. It is where preferences are
**written** — the SDK only ever reads them, and there is no other writer in the
system. Skip it and the enforcement path still works, but it resolves every call
to a **total refusal** — what nobody accepted is refused, on every option and
every provider — and every proof you collect says `legal_basis: "not_established"`:
nobody was asked.

That state is safe and it is honest. It is also not a product: your users are
all refusing capabilities you are paying for, and if your integration hands the
model tools, every call raises `ToolUseRefusedError`, because a refusal of
`agentic_tools` is enforced by declining the call rather than by rewriting a
parameter. Running the widget is what turns that into a question somebody can
answer.

Most of this page describes the *asked* mode of
[Concepts §4](./concepts.md#4-two-modes-asked-and-imposed), where the widget
asks and the person answers. The *imposed* mode uses the **same widget**, and
needs it more than it might seem: nobody is asked there, so everybody has to be
told, and this is the surface that tells them. [§11](#11-the-imposed-mode) is
what changes.

> **Status.** The widget is implemented and served by the ID Side cloud, at
> `https://sdk.idside.eu/widget/v1.js`. Everything below is the real interface;
> create an application and its publishable key in the dashboard
> (<https://sdk.idside.eu/dashboard/signup>) before integrating.
> See [release status](./README.md#status).

---

## 1. It is a service, not a package

There is nothing to `npm install`. You add one `<script>` tag pointing at ID
Side, exactly as you would for a consent management platform:

```html
<script src="https://sdk.idside.eu/widget/v1.js"></script>
```

That script creates a **cross-origin iframe** served by ID Side. Your page
cannot read inside it; the iframe cannot read your page. That isolation is not
an implementation detail that could be optimised away later — it is the
mechanism that makes the [opaque tunnel](./concepts.md#3-the-opaque-tunnel)
real. A widget shipped as a JavaScript library running in your page would put
your code between the user and their answer, and the signed proof would be worth
nothing.

Which is also why `packages/widget/` in this repository holds no source: the
widget is not code you install, run or vendor. This page is its interface.

> The script derives everything else — the iframe URL, the `postMessage` target
> origin — from its own `src` (`document.currentScript.src`), so it must be
> loaded with a real `<script src>` tag. Injecting its text through `eval` or an
> inline block leaves it with no origin and it refuses to start.
>
> **Take the tag from your dashboard, not from this page.** The dashboard emits
> it from the host actually serving your app, so it is right by construction.
> If you set `IDSIDE_BASE_URL` on your backend, give it that same service
> origin.

### If your pages send a Content-Security-Policy

Two directives, and the widget asks nothing else of your policy:

```http
Content-Security-Policy: script-src https://sdk.idside.eu; frame-src https://sdk.idside.eu
```

- **`script-src`** — the `<script src>` above. Refuse it and nothing else
  happens: your own `IDSide.init(...)` throws `ReferenceError: IDSide is not
  defined`.
- **`frame-src`** — the iframe, and every document it navigates to within
  itself (the handshake, then the panel — all on that one origin). If your
  policy has no `frame-src`, the browser falls back to `child-src` and then to
  `default-src`, so a bare `default-src 'self'` blocks the widget just as
  effectively. Add the origin to whichever directive your policy actually uses
  for frames.

The origin in both directives is the one serving the widget, and it changes
with the environment: a cloud on your own machine serves it from
`http://localhost:…`, production from the address on your dashboard. Allow the
origin of **every** environment your pages run against — in each environment's
policy, or all of them in one. Forget one and the script is blocked before the
widget can show any error at all: the violation report described below is
heard by the script, and a script that never loaded hears nothing.

Nothing else is needed: the embed script makes no network request of its own
from your page (so no `connect-src` entry), and it sizes and positions the
iframe through the CSSOM rather than a `style` attribute, which a strict
`style-src` does not reach. A page serving `default-src 'none'` plus those two
directives runs the widget — checked in Chromium, Firefox and WebKit.

A frame your policy refuses used to be the quietest failure in this
integration: Chromium fires `load` on the frame it blocked and WebKit fires
nothing at all, so the page saw a consent surface that never appeared, and the
only trace was one console line nobody was watching. The script now listens for
the browser's own violation report: `onError` receives a message naming the
directive to add and the origin to allow in it, `hasDecided()` rejects with the
same message, and — under a `pk_test_` key — that sentence appears in a box on
your own page ([§5](#5-the-publishable-key-and-your-declared-origins)). A
*report-only* policy is ignored, because the frame it complains about is
loading fine.

## 2. Initialize it

```html
<script src="https://sdk.idside.eu/widget/v1.js"></script>
<script>
  IDSide.init({
    publishableKey: "pk_test_your_publishable_key",
    userId: "usr_marie_456",
  });
</script>
```

That is the whole integration for the default case: a banner appears at the
bottom of the page — but only if there is something to ask. The widget decides
that for itself, server-side: someone who already answered, or whose browser
sent GPC, or who has a linked ID Side account, is not asked again. Consent
fatigue is manufactured by re-asking; the widget re-asks on the questions this
person has never been shown, and on nothing else.

`init()` returns a handle with one method:

```js
const widget = IDSide.init({ publishableKey, userId });
widget.destroy();   // removes the iframes, the listeners and any notice
```

**`init()` is idempotent, about one exact thing.** Called again with the same
`publishableKey`, `userId`, `mode`, `container` and `locale`, while its iframe
is still in the document, it returns the widget already running: same handle,
same frame, no second iframe and no second handshake. That is the component
that remounts on every navigation — Turbo, Stimulus, React, any SPA — and
there is nothing for you to orchestrate.

Called with anything else it **destroys the running widget and starts the new
one**: another `userId` above all (a sign-in, a sign-out, an account switch),
another mode, another container, another locale. It does the same when the
iframe has left the document (Turbo replacing `<body>`, a container unmounted)
and when the widget gave up — a frame that cannot report is not the widget you
asked for. Two live iframes never exist, and one person's frame is never left
answering for the next.

Three consequences worth knowing:

- the new call's callbacks **replace** the previous ones. A remounted
  component's `onUBT` must not be the unmounted component's, pointing at state
  that is gone;
- if the widget is already ready, the new `onReady` still fires — once, and
  asynchronously. A component mounting after the event would otherwise wait
  forever for a message already delivered;
- an `init()` that *throws* has already torn down what was running. `userId`
  going `undefined` is what a sign-out looks like from here, and leaving the
  previous person's consent surface live on a page that has stopped being
  theirs is the one outcome that must not happen.

`destroy()` removes the iframes and the listeners. A second call does nothing,
and a call on a handle whose widget has already been replaced does nothing
either — a framework that mounts the next component before cleaning up the last
one cannot tear down the widget it has just started. `onChange()` subscriptions
are not part of a handle: they outlive `destroy()` and hear the next `init()`'s
events, so unsubscribe when the code that subscribed goes away.

### Options

| Option | Type | Default | |
| --- | --- | --- | --- |
| `publishableKey` | string | — | **Required.** Your `pk_test_…` / `pk_live_…` key. See §5. |
| `userId` | string | — | **Required.** How you name this person. See §4 — read it before you invent one. |
| `mode` | `"banner"` \| `"modal"` \| `"inline"` | `"banner"`, or `"inline"` when `container` is given | The presentation. See §3. |
| `container` | CSS selector or element | — | Where an inline panel renders. Required for `mode: "inline"`. |
| `locale` | `"fr"` \| `"en"` | `"fr"` | The end user's language. See §6. |
| `onReady` | `() => void` | — | The widget has loaded. |
| `onConsentRecorded` | `({ source }) => void` | — | A decision was recorded, and through which channel. **Never what was decided.** |
| `onUBT` | `(token: string) => void` | — | ID Side issued a User Binding Token for this person. See §8. |
| `onError` | `({ message }) => void` | — | An integration error. Logged to the console if you pass nothing, and shown on the page itself under a `pk_test_` key. See §5. |

A missing `publishableKey` or `userId` throws immediately — the widget will not
start half-configured. An unrecognised `mode` silently falls back to `banner`
rather than rendering nothing: a consent surface that fails to appear collects
no answers, which is the one failure mode that must never be quiet.

## 3. Three presentations

| `mode` | What it looks like | Use it for |
| --- | --- | --- |
| `banner` | a card fixed at the bottom of the viewport, centred, sized to its content | the default first-visit surface, like a cookie banner |
| `modal` | a full-viewport overlay: a centred card on a desktop, a bottom sheet on a phone | when the answer must be given before continuing |
| `inline` | a panel rendered inside a container you provide | a settings or privacy page |

```js
// A permanent preference panel on your settings page.
IDSide.init({
  publishableKey: "pk_test_your_publishable_key",
  userId: "usr_marie_456",
  mode: "inline",
  container: "#privacy-settings",
});
```

In `banner` and `modal` the iframe starts hidden and **the widget alone decides
whether to appear**. Your page cannot open it with an answer already in it, and
cannot open it at all except through `IDSide.showPreferences()` (§7) — which
asks for the UI, and carries nothing else.

### What it asks, and what it only states

The widget asks a person only about options one of your providers can apply per
request. An option your organization's policy, or your application, already
refuses for everyone is not asked at all: it appears among what the application
decided, marked "Refused". A "yes" given to it before the refusal does not come
back on its own when the refusal is lifted — the question is asked again, and
until it is answered the option is refused. When there is no question left to
ask — every one already refused, or none your providers apply per request — the
banner has a single button, "Got it", which records that the person saw it and
issues a UBT (`onUBT`, §8) like any other answer. The `inline` panel offers the
same button until an answer is recorded.

## 4. What to send as `userId`

The widget refuses to start without a `userId`, and the SDK refuses to make a
call without one. It is the same identifier on both sides: it is what ties the
answer given in the browser to the AI call made by your backend.

ID Side never stores it. It is turned into a per-app pseudonym on arrival —
a one-way function — and the pseudonym is what lands in the database, the ledger
and the token's `sub`. That is a good property, and it has one consequence you
have to plan for: **ID Side cannot recognise a person you name differently, and
cannot help you name them consistently.** Only you can.

**If the person is signed in to your product**, send your own stable identifier
for them — the primary key, the account id, whatever survives a password reset.
Nothing else is needed.

**If nobody is signed in** — a marketing site, a public demo, a checkout before
account creation — you have to mint an identifier. Do it like this:

```js
// Your storage, your domain, your cookie policy. ID Side sets nothing.
let visitorId = localStorage.getItem("visitor_id");
if (!visitorId) {
  visitorId = "anon_" + crypto.randomUUID();
  localStorage.setItem("visitor_id", visitorId);
}

IDSide.init({ publishableKey: "pk_test_your_publishable_key", userId: visitorId });
```

A random value, written by you, in your own storage, under your own domain. It
is stable enough that the same browser gets the same answers back, and it
carries no information about who the person is.

**Do not use a browser fingerprint.** It is the obvious way to get a stable
identifier without storing anything, and it is the wrong one: a fingerprint
identifies a person across sites they never linked, which is precisely the
tracking this product exists to give people a lever against. A consent tool
built on covert identification would be arguing against itself, and the ledger
would be the record of it.

**What happens when an identifier changes** — cleared storage, a new browser, a
new device: the person is a stranger. Not an error, a defined state. Everything
is refused for them, the proof records `source: "app_default"` and
`legal_basis: "not_established"`, and the widget asks again. Nobody is
mistakenly treated as having agreed to something. That is the safe direction to
fail in, and it is the reason this is a plumbing problem rather than a
compliance one.

**In the imposed mode, this is not your choice to make.** An organization's
policy is resolved against people ID Side knows, so send **the person's work
email address, or an identifier registered with ID Side**. Anything else is a
subject the system has never met: the policy still applies — it always does —
but no legal basis is signed for that call, because none was established for
anyone. We cannot guess the mapping on your behalf. The pseudonymisation is
one-way, which is exactly the property that protects your users, and exactly why
it cannot be undone here.

## 5. The publishable key and your declared origins

Two keys, and only one of them belongs in a browser:

| Key | Where | Protected by |
| --- | --- | --- |
| `sk_live_…` / `sk_test_…` | your backend, given to `consent.wrap()` | secrecy |
| `pk_live_…` / `pk_test_…` | your pages, given to `IDSide.init()` | **the origin allowlist, not secrecy** |

The publishable key is public by design — it ships to every visitor and anyone
can read it. What stops someone lifting it onto their own site is that the
widget only runs on origins you declared in your ID Side dashboard.

An origin is `scheme://host[:port]`, exactly — `https://medhelp.example`,
`http://localhost:3000` — with no path and no trailing slash. Declare each one
you serve from, including the ports you develop on. The embed script reports the
embedding page's origin; an undeclared origin gets an integration error instead
of a widget, and the browser independently refuses to render the frame at all
(`Content-Security-Policy: frame-ancestors`, built from your list). Both checks
exist because the first one is a courtesy to the developer and the second one is
the actual boundary.

**Where that error actually shows up**, because it used to depend on the mode
and this page did not say so:

- ID Side renders a real error page — *"…is not a declared domain for this
  app"* — inside the iframe. In **`inline`** mode the panel is in your page at
  its own size, so you read it there;
- in **`banner`** and **`modal`** the iframe is `display:none` until the widget
  asks to be shown, and an error never asks. Nothing appeared on the page. So
  when your page runs a **`pk_test_`** key, the embed script now puts the same
  sentence in a dismissable box fixed to the bottom-left of your own page. It is
  a developer notice: it carries no preference and no user data, it can be
  clicked away, and `destroy()` removes it;
- under a **`pk_live_`** key nothing is drawn. A visitor is owed no technical
  notice about a misconfiguration, so in production it stays what it always
  was — `onError`, and the console when you pass none.

The same rule covers every integration failure: an undeclared origin, an invalid
publishable key, [a CSP that refused the
frame](#if-your-pages-send-a-content-security-policy), and an origin that never
answered at all. All four reach `onError`, settle `hasDecided()` and, with a
test key, appear on the page.

`frame-ancestors` is checked against **every** ancestor of our frame, not only
the page that embeds it. If your page is itself framed by another site — a
portal, a marketplace back-office, a Teams tab — declare that site's origin too.
Otherwise the browser refuses to render the widget, and this is the one failure
neither side can report: the ancestor check happens on our response, so your
page gets no error event and `onError` is never called. It shows up as a frame
that stays empty, with a `frame-ancestors` line in the console.

## 6. Language

`locale: "fr"` or `locale: "en"`. French is the default; an unrecognised value
falls back to it. Pass whatever your page is already rendering in:

```js
IDSide.init({
  publishableKey: "pk_test_your_publishable_key",
  userId: visitorId,
  locale: document.documentElement.lang.startsWith("en") ? "en" : "fr",
});
```

The widget keeps its own locale rather than reading a session: it is a
third-party iframe, and a visitor reading your page has not asked ID Side to
remember anything about them.

## 7. The rest of the API

```js
IDSide.mount("#privacy-panel");     // an extra inline panel, alongside init()'s
IDSide.showPreferences();           // reopen the preference centre
await IDSide.hasDecided();          // → boolean
const off = IDSide.onChange(handler); off();   // subscribe / unsubscribe
```

- **`mount(container)`** renders an additional inline panel in a container you
  provide — a settings page in a site whose banner is already running. `init()`
  first; it throws otherwise. Its return value is internal, so ignore it.
- **`showPreferences()`** asks the widget to open. It is the **only** instruction
  your page may send it, and it carries nothing: there is no argument, and no way
  to open the panel with an answer pre-filled. When the main surface is inline,
  it scrolls the panel into view instead.
- **`hasDecided()`** resolves to one bit — *has this person decided anything*,
  through the widget, GPC, or a linked account. Not what they decided. It is
  what you need to render "you have not set your preferences yet" without
  learning anything you must not learn. **It always settles**: it resolves when
  the widget reports the bit, and otherwise it *rejects* — the iframe failed to
  load, `init()` was never called, the widget errored before reporting,
  `destroy()` ran first, or nothing happened at all within 15 seconds. So
  `await` it on a critical path, with a `catch`: a promise that cannot answer
  must not hang the page that awaited it. The reported bit wins over an earlier
  give-up, so a merely slow widget still resolves.
- **`onChange(callback)`** subscribes to the same lifecycle events as the
  `on*` options — `{ type: "decided", value }`, `{ type: "consent_recorded",
  source }`, `{ type: "ubt" }` — and returns a function that unsubscribes.
  Useful when several parts of a page need to react. Note the `ubt` event
  carries no token: only the `onUBT` option receives one.

## 8. From a recorded decision to a stronger proof

When the widget records a decision, ID Side may hand your page a **User Binding
Token**: a short JWT in which ID Side attests that *it* saw this user, in this
app, in this session, with this consent state.

```js
IDSide.init({
  publishableKey: "pk_test_your_publishable_key",
  userId: visitorId,
  onUBT: (token) => fetch("/api/session/ubt", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "content-type": "application/json",
      "x-csrf-token": csrfToken(),
    },
    body: JSON.stringify({ ubt: token }),
  }).then((response) => {
    if (!response.ok) throw new Error("Could not store the ID Side UBT");
    enableAICalls();
  }).catch((error) => disableAICalls(error)),
});
```

Send it to your backend and pass it to the SDK as `ubt=` **instead of**
`user_id=` (never both — that raises `ConflictingIdentityError`). The proof then
records `binding: "ubt"` rather than `binding: "user_id"`, and an auditor can
see the difference: with `user_id`, nothing ties the identifier you passed to
the person who actually answered; with a UBT, ID Side signed that binding
itself. See [Concepts §8](./concepts.md#8-naming-the-end-user-user_id-vs-ubt).

The session endpoint, `csrfToken()`, `enableAICalls()` and `disableAICalls()`
are application code you implement in your framework. The widget does not await
the callback: your application must control when AI requests become available.

Store the token in a server-side session tied to the same visitor identifier
used by `IDSide.init()`. Wait for the session endpoint to accept it before
enabling an AI request. Protect that endpoint with your normal session and
CSRF/origin checks; keep ID Side secret keys and provider keys on the backend.

The callback is emitted after a decision is recorded or the user explicitly
renews its binding in the widget. Reloading a page with an existing decision
does **not** automatically emit another `onUBT` callback. Keep the binding
across page loads, replace it after each new callback, and discard it when the
session's user changes. A stored UBT is not permanent: it expires after 24 hours,
and authorization failures must stop the AI call until a valid binding is
available again. The SDK tells those failures apart from a refused key:
`InvalidBindingError` for a UBT that did not verify — expired, issued for
another application, altered, or not a UBT at all — `BindingRequiredError` for
a call that passed `user_id` where a UBT is required
([the three 401s](./errors.md#three-401s-three-different-answers)).

For a strict visitor application, a user whose current choices are complete can
open `IDSide.showPreferences()` and select **Confirm these choices** to obtain
a new binding. This also works for preferences from a linked ID Side account
and a previously recorded, complete GPC refusal. It changes neither the choices
nor their source: the widget sends `POST /v1/widget/ubt`, then forwards the new
token through `onUBT`. No previous token is needed, so a lost backend session
can recover through this explicit action. The handshake and state checks have
the same evidentiary limits as the initial widget interaction; the button is
not an additional authentication factor.

If the choices or policy changed while the panel was open, renewal is refused
and the user must reload the page. An incomplete or outdated local consent must
be updated through the questionnaire first. Authorization also checks the
current coverage for every strict call and returns `409 consent_refresh_required`
when the current policy or required questions are no longer covered; your
backend must stop that AI request and ask the user to revisit the widget. The
SDK raises [`ConsentRefreshRequiredError`](./errors.md#consentrefreshrequirederror)
for it — not the outage error, because retrying changes nothing.

Declare every provider the application will call in its dashboard before
showing the widget. A provider introduced later can require new questions;
an incomplete consent in strict mode stops authorization, including the first
call to an undeclared provider. Declaring it makes those questions available
to the visitor.

If the application requires bound tokens, the backend must wait for a UBT:
`user_id` is rejected even before the first decision. A plain identifier is only
an option when the application permits it; it is never a fallback for an invalid
or expired UBT. For the organization-imposed mode, follow the application's
employee identity instructions instead of requiring visitor consent.

## 9. What your page can never do

The callbacks report **that** something happened, never **what** was chosen.
`onConsentRecorded` gives you the channel a decision came through and nothing
else; `hasDecided()` gives you one bit; there is no `getPreferences()`, on
either side of the tunnel.

This is the same invariant as the SDK's missing preference setter
([Concepts §3](./concepts.md#3-the-opaque-tunnel)), enforced by the same
mechanism at the other end. If your page could read the consent state, it could
also collect an opt-out and call the SDK as if it had an opt-in, and every ACT
token in the world would be worth exactly what that one is. The narrowness is
the product.

The widget also drops any `postMessage` that does not come from ID Side's origin
*and* from one of its own frames. Origin alone is not enough, and neither is
source alone.

## 10. GPC

The widget detects [Global Privacy Control](https://globalprivacycontrol.org/)
both ways — the `Sec-GPC: 1` request header, and `navigator.globalPrivacyControl`
in the page — and acts on it without anyone clicking anything. The resolved
consent state then carries `source: "gpc"`, at the same trust level as an
explicit answer: the user's own browser said it, and you were not in the loop.

GPC is a default, not a decision, which is why an explicit answer given in the
widget wins over it — including an answer that opts back in for your site
specifically. The GPC specification allows exactly that.

## 11. The imposed mode

When an application serves an organization's own employees rather than its
visitors, the widget is **still the mandatory surface**, and everything above
still applies: same tag, same `init()`, same presentations, same origin rules.
What changes is that it stops asking and starts stating.

Falling silent was half an answer. "There is nothing for you to decide" says
nothing about *what was decided*, and an organization that decides for its
employees owes them the substance, not just the courtesy of not being asked. So
the panel renders, server-side, in the widget's own locale:

- **the legal basis, in words** — never the column value the proofs carry. This
  is the single thing the person is most owed, and `not_established` in
  particular has to be readable, because it means the proofs issued about them
  assert nothing;
- **what was decided, labelled** — each option allowed or refused, using the
  same shared catalogue the SDKs and the [provider
  matrix](./providers-privacy-matrix.md) are generated from, so what an employee
  reads and what a developer reads cannot drift apart. Only what the resolution
  actually holds: a policy produces refusals, not a complete state, and walking
  the whole catalogue would show inventions next to facts. Refusing nothing is
  itself a decision, and says so;
- **a link to the ID Side workspace**, in a new tab, for what this iframe
  cannot know and must not: who decided, when, and through which team.

**Read-only by construction, not by convention.** The switches are built by the
script from the question list, and on such an app that list is emptied
server-side — so there is no second place a switch could come from. A switch
that governed nothing would be worse than the silence it replaced: it would
manufacture the appearance of the very consent this mode exists not to pretend
to. For the same reason the account-linking affordances are not rendered, and
`POST /v1/consent` answers `409 consent_not_collected` to any write, whatever
`source` it claims — GPC included.

The SDK side is unchanged: it reads `source` and `legal_basis` as opaque
strings, and nothing in it branches on the mode. See [Concepts
§4](./concepts.md#4-two-modes-asked-and-imposed) for why this mode is not a
shortcut past the first, and why you must never point it at your own visitors.

## Next

- [Concepts §4](./concepts.md#4-two-modes-asked-and-imposed) — asked and imposed, and why the second is not a shortcut past the first
- [Concepts §8](./concepts.md#8-naming-the-end-user-user_id-vs-ubt) — `user_id` vs UBT on the SDK side
- [Python quickstart](./quickstart-python.md) · [Node.js quickstart](./quickstart-node.md) — the other half of the integration
- [What each provider actually lets you control](./providers-privacy-matrix.md) — which of the questions the widget asks can be enforced at all
