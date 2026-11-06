// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * The consent options an app can actually put to its users.
 *
 * A developer wrapping OpenAI and Gemini needs to know what to render in their
 * consent UI, and the honest answer depends on which providers they wrap. This
 * module computes it from the shared catalogue, offline, with no network call.
 *
 * The rule that shapes everything here:
 *
 *   A question reaches an end user only if at least one of the app's providers
 *   can enforce it PER REQUEST.
 *
 * An account-level control lives on the developer's API key and applies to all
 * of their users at once. Asking one person about it would manufacture the
 * appearance of a choice nobody has — two users answering differently would be
 * treated identically. Those become *disclosures*: facts about how this app is
 * configured, which the developer must be able to state and which the ledger
 * records, but which no consent dialog may present as a choice.
 *
 * The exception is `routable`: an account-level control becomes a genuine
 * per-user one when the developer provisions several credentials with different
 * privacy postures, because the SDK can then pick the one matching each answer.
 * That is the only route by which training opt-out can ever be honoured per
 * user — no provider exposes it as a request parameter.
 */

import { createRequire } from "node:module";
import { rawDescriptors } from "./descriptors.ts";

const require_ = createRequire(import.meta.url);

export type EnforcementLevel = "enforced" | "contractual" | "account" | "absent" | "unsupported";

export interface ConsentOption {
  readonly name: string;
  readonly group: string;
  readonly labelEn: string;
  readonly labelFr: string;
  readonly descriptionEn: string;
  readonly descriptionFr: string;
  /** Always `false`: an option nobody has answered is refused, never assumed. */
  readonly default: boolean;
  readonly legalBasis: string;
  readonly gdpr: string;
  /** Which of the app's providers will honour an answer to this, per request. */
  readonly enforcedBy: readonly string[];
  /**
   * Providers that will honour it by ROUTING the call to the credential carrying
   * that posture, rather than by a request parameter. The answer is as real to
   * the user either way; the mechanism differs, and a consent UI that explains
   * itself may want to say which.
   */
  readonly routedBy: readonly string[];
}

export interface Disclosure {
  readonly option: ConsentOption;
  /**
   * `guaranteed` — already true for everyone, in the user's favour: the terms
   *   of every provider that answers it rule the processing out (`contractual`
   *   and `protective`). Terms that allow or impose it are no guarantee, and
   *   neither is one provider among several whose terms do.
   * `beyond_user_control` — the app's own configuration decides, or the
   *   provider's terms do (`contractual` terms that allow or impose it, or leave
   *   it to the developer's organization): no answer in a dialog could change it.
   * `not_offered` — no control for it is reachable through the endpoints the
   *   descriptors wrap, whether the processing is `absent` from the API or
   *   `unsupported` there. Nobody decided anything; saying "your app
   *   configured this" about a feature the API does not have would be plainly
   *   false.
   *
   * Snake_case because these are VALUES shared with the Python SDK, not
   * TypeScript field names — anything that logs or branches on them must agree
   * across both SDKs.
   */
  readonly kind: "guaranteed" | "beyond_user_control" | "not_offered";
  readonly byProvider: Readonly<Record<string, EnforcementLevel>>;
  /** Providers where a second credential would give users the choice back. */
  readonly routableFrom: readonly string[];
}

interface RawOption {
  name: string;
  group: string;
  label_en: string;
  label_fr: string;
  description_en: string;
  description_fr: string;
  default: boolean;
  legal_basis: string;
  gdpr: string;
}

interface Catalogue {
  options: RawOption[];
  groups: { name: string; label_en: string; label_fr: string }[];
}

interface EnforcementEntry {
  level: EnforcementLevel;
  param?: string;
  mechanism?: string;
  conditions?: string;
  protective?: boolean;
  routable?: boolean;
  note: string;
}

let catalogueCache: Catalogue | undefined;

/** The shared option catalogue, as shipped with this package. */
export function catalogue(): Catalogue {
  catalogueCache ??= require_("./consent-options.json") as Catalogue;
  return catalogueCache;
}

/**
 * Enforcement, read from the statically imported descriptors.
 *
 * `decidableOptions` is public API taking caller-supplied strings. This used to
 * interpolate one straight into a `require`, so
 * `decidableOptions(["../../../etc/passwd"])` loaded and cached an arbitrary
 * file whose contents then flowed into the returned options — the `catch` hid
 * the error, it did not stop the read. The fix was an allowlist; the better fix
 * is having no path to build at all. A hardcoded allowlist also silently went
 * stale the first time a descriptor was added.
 */
function enforcementFor(provider: string): Record<string, EnforcementEntry> {
  // A build may meet a provider it has never heard of. Contributing nothing is
  // right; throwing would take down a consent dialog over a name.
  const descriptor = rawDescriptors().get(provider);
  return (descriptor?.enforcement as Record<string, EnforcementEntry>) ?? {};
}

/**
 * A posture nobody can be routed away from is not a choice. One credential is
 * the app's single posture, whatever it is labelled; it takes a second for two
 * users to be treated differently.
 */
const MIN_POSTURES_FOR_A_CHOICE = 2;

/** The providers where the developer really can send two users two ways. */
function routingProviders(routed: Readonly<Record<string, readonly string[]>> | undefined): Set<string> {
  const found = new Set<string>();
  for (const [provider, postures] of Object.entries(routed ?? {})) {
    if (new Set(postures).size >= MIN_POSTURES_FOR_A_CHOICE) found.add(provider);
  }
  return found;
}

function toOption(
  raw: RawOption,
  enforcedBy: readonly string[],
  routedBy: readonly string[] = [],
): ConsentOption {
  return {
    name: raw.name,
    group: raw.group,
    labelEn: raw.label_en,
    labelFr: raw.label_fr,
    descriptionEn: raw.description_en,
    descriptionFr: raw.description_fr,
    default: raw.default,
    legalBasis: raw.legal_basis,
    gdpr: raw.gdpr,
    enforcedBy,
    routedBy,
  };
}

/**
 * The questions this app may actually put to a user, in catalogue order.
 *
 * Ordered by the catalogue rather than by provider, so the dialog never depends
 * on which client happened to be wrapped first, and each question is asked once
 * however many providers share it.
 *
 * `routed` maps a provider to the posture labels the app provisioned for it —
 * the keys of the `credentials` option given to `consent.wrap()`. An
 * account-level option marked `routable` in the descriptor becomes a real
 * question for that provider once there are TWO of them, because two users
 * answering differently can then be treated differently. With one, the app has
 * one posture and asking would manufacture a choice nobody has.
 */
export function decidableOptions(
  providers: readonly string[],
  routed?: Readonly<Record<string, readonly string[]>>,
): ConsentOption[] {
  const levels = new Map(providers.map((p) => [p, enforcementFor(p)]));
  const routing = routingProviders(routed);

  return catalogue()
    .options.map((raw) => {
      const enforcedBy = [...levels]
        .filter(([, e]) => e[raw.name]?.level === "enforced")
        .map(([name]) => name);
      const routedBy = [...levels]
        .filter(([name, e]) => routing.has(name) && !enforcedBy.includes(name) && e[raw.name]?.routable)
        .map(([name]) => name);
      return enforcedBy.length > 0 || routedBy.length > 0 ? toOption(raw, enforcedBy, routedBy) : undefined;
    })
    .filter((o): o is ConsentOption => o !== undefined);
}

/** The levels at which no control exists, whatever the reason. */
const NO_CONTROL: ReadonlySet<EnforcementLevel> = new Set(["absent", "unsupported"]);

/**
 * Mirrors `_kind_for` in the Python SDK — these values cross SDK boundaries.
 *
 * `contractual` alone says the terms decide, not which way: it used to be read
 * as a guarantee, which called Anthropic's review of flagged content and
 * OpenAI's "unless the org opts in" guaranteed to the user.
 */
function kindFor(entries: EnforcementEntry[]): Disclosure["kind"] {
  if (entries.every((entry) => entry.level === "contractual" && entry.protective === true)) return "guaranteed";
  if (entries.every((entry) => NO_CONTROL.has(entry.level))) return "not_offered";
  return "beyond_user_control";
}

/**
 * What the user cannot decide — stated, never asked.
 *
 * `routed` is the same argument {@link decidableOptions} takes, and moves the
 * same options: anything the app can now route on becomes a question, so it
 * stops being a disclosure. Passing it to one and not the other would say two
 * contradictory things about the same option.
 */
export function disclosures(
  providers: readonly string[],
  routed?: Readonly<Record<string, readonly string[]>>,
): Disclosure[] {
  const levels = new Map(providers.map((p) => [p, enforcementFor(p)]));
  const asked = new Set(decidableOptions(providers, routed).map((o) => o.name));
  const found: Disclosure[] = [];

  for (const raw of catalogue().options) {
    if (asked.has(raw.name)) continue;

    const entries: [string, EnforcementEntry][] = [];
    for (const [name, enforcement] of levels) {
      const entry = enforcement[raw.name];
      if (entry?.level) entries.push([name, entry]);
    }
    if (entries.length === 0) continue;

    found.push({
      option: toOption(raw, [], []),
      kind: kindFor(entries.map(([, entry]) => entry)),
      byProvider: Object.fromEntries(entries.map(([name, entry]) => [name, entry.level])),
      routableFrom: entries.filter(([, entry]) => entry.routable).map(([name]) => name),
    });
  }

  return found;
}
