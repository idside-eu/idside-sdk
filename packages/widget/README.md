# Preference Center widget

**The widget is a hosted service, not a package in this directory.** There is no
source here because there is none to ship: you do not install, vendor or run the
Preference Center — you add one `<script>` tag pointing at ID Side, the way you
would for a consent management platform.

```html
<script src="https://idside.eu/widget/v1.js"></script>
<script>
  IDSide.init({ publishableKey: "pk_test_…", userId: "usr_marie_456" });
</script>
```

**→ [Integration guide](../../docs/widget.md)** — the tag, `IDSide.init()` and
its options, the three presentations (banner, modal, inline), the locale, what
to send as `userId`, and how the publishable key relates to the origins you
declare.

That the widget is served by ID Side and framed cross-origin is the whole point,
not a deployment convenience. Your page cannot read inside the iframe and the
iframe cannot read your page, which is what makes the
[opaque tunnel](../../docs/concepts.md#3-the-opaque-tunnel) real: a widget
running as a library *in* your page would put your code between the user and
their answer, and the signed proof would attest to nothing. The same isolation
is why this repository can be fully auditable without the widget's markup being
in it — every line that touches *your requests* is here; the surface that
collects *your users' answers* is deliberately somewhere you cannot reach.

It is complete: three presentations, French and English, automatic
[GPC](../../docs/concepts.md#10-gpc) detection, and a host-page API
(`IDSide.init()`, `mount()`, `showPreferences()`, `hasDecided()`, `onChange()`)
that reports *that* a decision was recorded and never *what* was decided.

See also the [architecture — circuit A](../../docs/ARCHITECTURE.md#2-the-two-circuits).
