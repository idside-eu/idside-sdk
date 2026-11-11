# What each AI provider actually lets you control

> **Generated from `packages/descriptors/` — do not edit by hand.**
> Run `python packages/descriptors/build_matrix.py` after changing a descriptor.
> A test fails the build if this file and the descriptors disagree.

This is the heart of the product: the honest answer to *"if a user refuses
this, what actually happens?"* — provider by provider, option by option.

## How to read the levels

| | Meaning |
| --- | --- |
| ✅ **enforced** | A real request parameter the SDK sets. The provider's API accepts it and it changes behaviour. The only level that is technically guaranteed. |
| 📄 contractual | No request parameter exists; the provider's written terms govern it. Which way they decide is the entry's `protective`: `true` where they rule the processing out for every call, `false` where they allow it, impose it or leave it to the developer's organization — and only the first is a guarantee to the user. The ACT token is the proof that the choice was collected and transmitted, not the mechanism that applies it. |
| ⚙️ account-level | Settable, but only in the provider's account or console — outside the API, so the SDK cannot apply it per request. Must be surfaced honestly to the developer. Marked `routable` when the developer can provision one credential per posture and let the SDK choose between them per request — which turns it back into a genuine per-user control. |
| ➖ absent | The provider does not perform this processing on the API this descriptor wraps, so there is nothing to refuse: the answer changes nothing because nothing would have happened. Said only where the descriptor's note states why the wrapped API performs no such processing, and `unsupported` otherwise. What an application builds on its side with the same data is its own processing, which no descriptor describes. |
| ❌ unsupported | This descriptor knows no way to honour this choice on the endpoints it wraps — no request parameter, written commitment or account setting — and nothing establishes that the processing is absent. Saying so plainly is worth more to a DPO than pretending otherwise. |

## Three things that are true of every provider

**No absolute zero.** Content flagged by trust & safety survives every zero-retention arrangement (up to 2 years at one provider; CSAM detections and abuse logs elsewhere). No option here may be worded as an absolute.

**Tools reintroduce retention.** Agentic tools are excluded from zero-retention arrangements at every provider that offers one, and the APIs do not block them — using one is the choice. This is why `agentic_tools` is a per-request option and not an account setting.

**Contracts are the real guardrail.** A DPA plus a retention amendment is what binds a provider. Request parameters only stop the application from contradicting the contract it signed. Say this plainly rather than implying the parameters are the protection.

## The matrix

| Option | Anthropic | Deepseek | Gemini | Mistral | Openai |
| --- | --- | --- | --- | --- | --- |
| **`training.foundation`** | 📄 contractual<br>*terms do not rule it out* | ❌ unsupported | ⚙️ account-level | ⚙️ account-level | 📄 contractual<br>*terms do not rule it out* |
| **`training.personalisation`** | ❌ unsupported | ❌ unsupported | ⚙️ account-level | ⚙️ account-level | ➖ absent |
| **`training.analytics`** | 📄 contractual<br>*terms do not rule it out* | ❌ unsupported | ⚙️ account-level | ⚙️ account-level | 📄 contractual<br>*terms do not rule it out* |
| **`memory`** | ❌ unsupported | ➖ absent | ➖ absent | ➖ absent | ➖ absent |
| **`agentic_tools`** | ✅ **enforced**<br>*SDK refuses the tool* | ✅ **enforced**<br>*SDK refuses the tool* | ✅ **enforced**<br>*SDK refuses the tool* | ✅ **enforced**<br>*SDK refuses the tool* | ✅ **enforced**<br>*SDK refuses the tool* |
| **`data_residency`** | ⚙️ account-level | ❌ unsupported | ❌ unsupported | ⚙️ account-level | ⚙️ account-level |
| **`retention.conversation`** | 📄 contractual<br>*terms do not rule it out* | ❌ unsupported | ❌ unsupported | ✅ **enforced**<br>`store` | ✅ **enforced**<br>`store`, `background`<br><sub>`background`: `false` only; `background: true` is additionally rejected outright in the EU region</sub> |
| **`retention.cache`** | 📄 contractual<br>*terms do not rule it out* | ❌ unsupported | ⚙️ account-level | ❌ unsupported | ✅ **enforced**<br>`prompt_cache_retention`<br><sub>`prompt_cache_retention`: values `in_memory` or `24h`; on GPT-5.5+ only `24h` is supported, so the REFUSAL cannot be honoured from that model onward</sub><br><sub>`prompt_cache_retention`: forced ONLY on models matching `gpt-4*\|gpt-5\|gpt-5-*\|gpt-5.1\|gpt-5.1-*\|gpt-5.2\|gpt-5.2-*\|gpt-5.3\|gpt-5.3-*\|gpt-5.4\|gpt-5.4-*\|o1*\|o3*\|o4*` — anywhere else, including any model released after this list was written, the call goes out unforced and this option is not enforced there</sub> |
| **`retention.files`** | ❌ unsupported | ❌ unsupported | ❌ unsupported | ❌ unsupported | ❌ unsupported |
| **`location`** | ❌ unsupported | ❌ unsupported | ❌ unsupported | ❌ unsupported | ❌ unsupported |
| **`human_review`** | 📄 contractual<br>*terms do not rule it out* | ❌ unsupported | 📄 contractual<br>*terms do not rule it out* | 📄 contractual<br>*terms do not rule it out* | 📄 contractual<br>*terms do not rule it out* |

## What the user is asked

Options are grouped by **purpose, not by provider**: a person decides what may
be done with their data, not which vendor may do it. When an app uses several
providers, the widget shows the union of the relevant options — once each.

### How my data trains AI — *Comment mes données entraînent l'IA*

#### `training.foundation`

> **Train general AI models on my conversations**<br>
> *Entraîner les modèles d'IA généraux sur mes conversations*

Your prompts and the answers you receive may be used to train the provider's general-purpose models, which serve every other user.

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: consent · Art. 6(1)(a); purpose limitation Art. 5(1)(b)
- **Why it is its own option**: The highest-stakes choice across every provider, and the one handled most inconsistently.

#### `training.personalisation`

> **Personalise my experience from my history**<br>
> *Personnaliser mon expérience à partir de mon historique*

Your data may be used to tailor answers to you specifically. This does not feed the general models.

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: consent · Art. 6(1)(a); profiling Art. 22 where decisions follow

#### `training.analytics`

> **Aggregated statistics to improve the service**<br>
> *Statistiques agrégées pour améliorer le service*

Anonymous, aggregated measurements. No individual content is retained.

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: legitimate_interest · Art. 6(1)(f), balancing test required
- **Why it is its own option**: Separated out precisely because providers bundle it with foundation training; a user who refuses model training has not necessarily refused this.

### What is remembered about me — *Ce qui est mémorisé sur moi*

#### `memory`

> **Remember me between conversations**<br>
> *Se souvenir de moi d'une conversation à l'autre*

The assistant keeps a persistent memory of you across sessions.

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: consent · Art. 6(1)(a); Art. 9 if special categories are inferred
- **Why it is its own option**: Structurally the highest re-identification risk in the AI context: enabling memory creates a behavioural profile anchored to an identified account.

### What connected apps and agents may do — *Ce que les applications et agents connectés peuvent faire*

#### `agentic_tools`

> **Let the AI use tools that store my data elsewhere**<br>
> *Autoriser l'IA à utiliser des outils qui stockent mes données ailleurs*

Web search, code execution, file stores, connectors and autonomous agents. These bring retention back in even when everything else is switched off.

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: consent · Art. 6(1)(a); Art. 28 where the tool is a processor
- **Why it is its own option**: THE per-request lever. Providers publish per-feature retention eligibility (code execution, Files, Batch, MCP servers are all excluded from zero-retention arrangements) and do not block their use — using them is the choice. The SDK sees `tools` on the call, so this is the one axis it can genuinely enforce everywhere.

### How long it is kept — *Combien de temps c'est conservé*

#### `retention.conversation`

> **Store my conversations on the provider's servers**<br>
> *Conserver mes conversations sur les serveurs du fournisseur*

Server-side history so the assistant can resume where you left off. Note: no provider offers absolute zero — content flagged by trust & safety is retained regardless (up to 2 years at one provider).

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: consent · Art. 5(1)(e) storage limitation
- **Why it is its own option**: The most widely available request-level control: OpenAI `store`, Mistral `store` on /v1/conversations, Gemini `store` on the Interactions API, xAI `store_messages`.

#### `retention.cache`

> **Keep my prompts in a fast cache**<br>
> *Garder mes requêtes dans un cache rapide*

Prompts are cached to speed up and cheapen repeated calls, typically for up to 24 hours. Note: no provider offers absolute zero — content flagged by trust & safety is retained regardless (up to 2 years at one provider).

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: consent · Art. 5(1)(e)
- **Why it is its own option**: Separated from conversation storage because the levers differ (OpenAI prompt_cache_options.ttl, Vertex project cacheConfig) and a user may reasonably accept one and not the other.

#### `retention.files`

> **Keep files I upload on the provider's servers**<br>
> *Conserver sur les serveurs du fournisseur les fichiers que j'envoie*

Documents and images sent to the AI are stored until deleted. Note: no provider offers absolute zero — content flagged by trust & safety is retained regardless.

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: consent · Art. 5(1)(e) storage limitation
- **Why it is its own option**: Uploaded files outlive the conversation that carried them, and are excluded from zero-retention arrangements at every provider that offers one. OpenAI exposes `expires_after` per upload and on vector stores — where the file's CONTENT lives, chunked and indexed — and Mistral exposes `expiry` and `visibility`; all of them on endpoints the SDK does not yet wrap.

### What is collected about me — *Ce qui est collecté sur moi*

#### `location`

> **Use my approximate location**<br>
> *Utiliser ma localisation approximative*

City or region level, inferred or declared, used to adapt answers.

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: consent · Art. 6(1)(a)
- **Why it is its own option**: No provider lets a request refuse it. The endpoint-by-endpoint check of 2026-07-26 found no location parameter a request could refuse at any of the five providers with a descriptor here. The pinned OpenAI client does carry one an application declares — `user_location`, in `web_search_options` on chat completions and on the `web_search` tool of Responses — and a refusal of `location` alone reaches nothing there: a user who refuses `agentic_tools` loses the web search, and the declared location with it. So there is no parameter for an SDK to force or route. It stays because it remains a real question at the APPLICATION layer — an app that collects a user's city and puts it in the prompt, or in a web-search tool, is doing exactly what this asks about — but the honest reading is that only the developer's own code can honour it, and every descriptor says `unsupported` for that reason rather than for want of looking.

#### `human_review`

> **Allow humans to review my conversations**<br>
> *Autoriser la relecture humaine de mes conversations*

Provider staff or contractors may read your exchanges, typically for quality or safety.

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: consent · Art. 6(1)(a); Art. 9 for special categories
- **Why it is its own option**: Consistently the control users are most surprised to learn exists.

### Where my data is processed — *Où mes données sont traitées*

#### `data_residency`

> **Keep processing inside my region**<br>
> *Garder le traitement dans ma région*

Where the AI actually runs. Refusing regional processing may mean your data is handled outside your jurisdiction.

- **Default**: refused — an option nobody has answered is never assumed
- **Legal basis**: consent · Chapter V — international transfers
- **Why it is its own option**: No provider forces it per request. Anthropic's `inference_geo` is a real body parameter, but its two settings (`global`, `us`) answer 'inside my region' for nobody whose region we know, and we never learn one — `location` is unsupported everywhere on purpose. OpenAI chooses residency by domain (eu./gb./ca.…), which is a property of the credential, not of the call. It stays its own option because it is `routable`: a developer holding one credential per region can have the SDK send each user's calls to the matching one — and because the SDK can read the base URL and record which region a call actually ran in.

## Provider by provider

### Anthropic

- **Client**: `anthropic`
- **Consent parameters injected into**: kwargs
- **Never overridden** (protected): `model`, `messages`, `system`, `stream`, `cache_control`, `container`
- **Verified**: 2026-07-26

**Endpoints wrapped, and what can be forced on each**

| Endpoint | Can force |
| --- | --- |
| `messages.create` | — nothing |

Two endpoints of one provider do not accept the same parameters, so the allowlist is per endpoint. The SDK tells the cloud which one it is about to call, and the cloud decides accordingly — forcing a parameter where it does not exist would break the call for exactly the users who opted out, and for nobody else.

**Technically enforced (1/11)**

- **`agentic_tools`** via the SDK refuses the tool
  - ENFORCED by the SDK, not by a parameter. Every zero-retention arrangement excludes agentic features while the API itself does not block them: a call that hands the model tools falls outside the retention regime the user was promised, and the SDK is the only component that can see it happening, because it is the only one that sees the outgoing request. When the user has refused, the cloud returns a `tools` constraint and the SDK refuses the call (`ToolUseRefusedError`) or strips the tools, per the constraint. Refusing is the default: an agent stripped of its tools does not fail, it answers wrongly, and the developer has no way to know. Note what is NOT sent to us to make this work: nothing. The constraint follows from the consent state alone — we are never told whether a given call carried tools. Bounded by what the SDK reads: `tools_param` plus the call site's `tool_fields`, which name `mcp_servers` — a SIBLING of `tools` that hands execution to a third party and is excluded from zero retention just as tools are. A refusal read from `tools` alone did not see it, and `extra_body` carried it straight past; per-call options able to rewrite the body are now refused outright. The rule to carry forward: any parameter that delegates execution to a third party is a tool, whatever it is called.

<details><summary>Everything else, and why</summary>

**📄 contractual**

- `training.foundation` — No per-request parameter. The Commercial Terms say Anthropic may not train models on Customer Content, but the Service Specific Terms (section E, Development Partner Program) let a customer opt in by enabling the permissive data opt-in setting, after which Anthropic may use the data it submits, including to train models. The developer's organization decides, as with OpenAI's "unless you explicitly opt in", and no consent answer changes it; the ACT token is the proof the choice was collected and transmitted. Checked on 2026-10-12 against Anthropic's Commercial Terms and its Service Specific Terms (effective 2026-08-31).
- `training.analytics` — Same terms, and the same exception: the Development Partner Program (Service Specific Terms, section E) lets the customer's organization opt the data it submits in, for Anthropic's products and services. Checked on 2026-10-12.
- `retention.conversation` — No store parameter. Retention is governed by the arrangement; Claude Fable 5 and Mythos 5 are Covered Models with a mandatory 30-day retention that zero-retention cannot override.
- `retention.cache` — Prompt caching keeps KV tensors and cryptographic hashes in memory for the TTL. Structured outputs cache the JSON schema for up to 24h — never put personal data in property names, enums, const or pattern. The TTL IS a request parameter here — `cache_control: {type: 'ephemeral', ttl: '5m' | '1h'}` at the root of `messages.create` — but the lever points the other way round from OpenAI's: Anthropic's cache is opt-in, so without `cache_control` there is no cache at all, and FORCING it would create the retention this option exists to refuse. Honouring a refusal therefore means REMOVING an argument the developer passed, which is the tool-refusal mechanism and not `params_forced`; the SDK has no such removal for `cache_control` today, so this stays `contractual` rather than claiming one. `cache_control` is in `protected_params` meanwhile, so the cloud can never write it. Capping an accepted cache at `5m` instead of `1h` would be a genuine forcing, and is the piece worth building first.
- `human_review` — No request parameter. Anthropic says no personnel can read retained conversations by default and that human review happens only through a controlled access path — for example when its automated trust and safety systems flag content, which it may then keep up to 2 years whatever the arrangement, zero data retention included. On Covered Models its Service Specific Terms (section F) add that it may retain and perform safety reviews on inputs and outputs, superseding any modified retention commitment such as zero data retention. Nothing rules the review of flagged content out. Checked on 2026-10-12 against Anthropic's Service Specific Terms, its zero-data-retention guide and its privacy-centre articles on retention and Covered Models.

**⚙️ account-level**

- `data_residency` — Two different questions, and the answers differ — which is why this entry read as self-contradictory until it was spelled out.

  PER REQUEST: no. `inference_geo` is real, lands in the body, and was verified live; what does not exist is a VALUE to force. Its two settings are `global` (anywhere) and `us`, the option asks "keep processing inside my region", and neither answers that for a European user — who is the one this product is built for. Mapping the question to a value needs the user's region, which we never learn: `location` is `unsupported` at every provider, on purpose. Forcing `us` on a European who asked to stay in their region would be worse than forcing nothing.

  BY CREDENTIAL: yes, hence `routable: true`. `workspace_geo` is fixed when a workspace is created and `allowed_inference_geos` is a workspace setting, so a developer holding one workspace per posture lets the SDK send each call to the matching one. That path is the developer's to provision and it needs nobody to know anyone's region.

  So the option stays out of the popup and stays offered to the developer as routable. Re-declaring it `enforced` would need a decision about what "my region" means when the provider offers two, and that is a product call rather than a missing line of code. Until 2026-07-27 it was declared `enforced`, the popup asked it, and no rule forced anything: a refusal that reached nothing, recorded in the ledger as though it had been honoured.

  VERIFIED by request: `global` (default) or `us`, rejected with 400 on models before Claude 4.6 (confirmed on sonnet-5, refused on haiku-4-5 and sonnet-4-5). The response reports `usage.inference_geo`, so what actually happened is verifiable. Billed x1.1 for `us`. Not available through the OpenAI-compatibility endpoint. This descriptor injected it into HEADERS until 2026-07-26, where Anthropic does not read it — the call succeeded and the residency choice did not apply. `param_target` is `kwargs` now.

**❌ unsupported**

- `training.personalisation` — No parameter this SDK can send. The pinned client's BETA Messages signature carries a `user_profile_id`, whose semantics are not publicly documented as of 2026-07-26 — noted so this line is not read as 'the API has no such concept', which is no longer safe to claim. The beta endpoint is not wrapped, and the transport guard refuses it.
- `memory` — Not a parameter. Anthropic's `memory` feature reaches the API as a TOOL (`{"type": "memory_..."}` inside `tools`), so a user who refuses `agentic_tools` already loses it through the tool refusal. What is NOT possible is honouring a refusal of memory alone: the mechanism drops the whole `tools` argument, it cannot remove one entry from it. Declaring this enforced on the strength of the tool refusal would promise a granularity we do not have.
- `location` — Not exposed as a request parameter.
- `retention.files` — No per-request file expiry control on the endpoints this descriptor wraps.

</details>

### Deepseek

- **Client**: `openai`
- **Consent parameters injected into**: kwargs
- **Never overridden** (protected): `model`, `messages`, `stream`
- **Verified**: 2026-07-26

**Endpoints wrapped, and what can be forced on each**

| Endpoint | Can force |
| --- | --- |
| `chat.completions.create` | — nothing |

Two endpoints of one provider do not accept the same parameters, so the allowlist is per endpoint. The SDK tells the cloud which one it is about to call, and the cloud decides accordingly — forcing a parameter where it does not exist would break the call for exactly the users who opted out, and for nobody else.

**Technically enforced (1/11)**

- **`agentic_tools`** via the SDK refuses the tool
  - ENFORCED by the SDK, not by a parameter — and the only thing on this list that is. It works here for the same reason it works everywhere: the SDK sees the outgoing request and can refuse it. Note the shape of that: the one control we can offer a DeepSeek user is the one we built ourselves.

<details><summary>Everything else, and why</summary>

**➖ absent**

- `memory` — Not an API capability. `absent` for that reason: the API has no memory for a refusal to switch off.

**❌ unsupported**

- `training.foundation` — The terms reserve the right to use API inputs and outputs to train and improve the models, and no request parameter or account setting removes it. There is nothing to force and nothing to promise — which is itself the finding a DPO needs.
- `training.personalisation` — Same clause, no lever.
- `training.analytics` — Same clause, no lever.
- `data_residency` — Data is processed in the People's Republic of China, with no regional endpoint and no parameter. For a GDPR deployment the honest recommendation is not to call api.deepseek.com at all: self-host the open weights, or use a Western host (Bedrock, Together, Fireworks), which is a different provider and gets its own descriptor. This descriptor exists so that an app doing it anyway is at least measured and journalled.
- `retention.conversation` — No `store` equivalent is documented, and the client's `store` parameter is OpenAI's, not DeepSeek's. Settled live on 2026-07-26: sending it returns 200 and does nothing — see `forceable_params_note`. Accepted and discarded is the one result that would let a false `enforced` survive testing, so it is written down here rather than left implied.
- `retention.cache` — Context caching is automatic and billed as a discount; it is not addressable per request.
- `location` — Not exposed as a request parameter.
- `human_review` — No documented commitment either way, which is not the same as a commitment. DeepSeek's Privacy Policy says it monitors interactions to improve its services and that support and safety-monitoring service providers access personal data on its behalf; neither it nor the Open Platform Terms of Service says whether people read API inputs. Refusing here changes nothing. Checked on 2026-10-12 against DeepSeek's Privacy Policy and Open Platform Terms of Service.
- `retention.files` — No per-request file expiry control on the endpoint this descriptor wraps.

</details>

### Gemini

- **Client**: `google.genai`
- **Consent parameters injected into**: kwargs
- **Never overridden** (protected): `model`, `contents`, `config`, `http_options`
- **Verified**: 2026-07-26

**Endpoints wrapped, and what can be forced on each**

| Endpoint | Can force |
| --- | --- |
| `models.generate_content` | — nothing |
| `models.generate_content_stream` | — nothing |

Two endpoints of one provider do not accept the same parameters, so the allowlist is per endpoint. The SDK tells the cloud which one it is about to call, and the cloud decides accordingly — forcing a parameter where it does not exist would break the call for exactly the users who opted out, and for nobody else.

**Technically enforced (1/11)**

- **`agentic_tools`** via the SDK refuses the tool
  - ENFORCED by the SDK, not by a parameter. Every zero-retention arrangement excludes agentic features while the API itself does not block them: a call that hands the model tools falls outside the retention regime the user was promised, and the SDK is the only component that can see it happening, because it is the only one that sees the outgoing request. When the user has refused, the cloud returns a `tools` constraint and the SDK refuses the call (`ToolUseRefusedError`) or strips the tools, per the constraint. Refusing is the default: an agent stripped of its tools does not fail, it answers wrongly, and the developer has no way to know. Note what is NOT sent to us to make this work: nothing. The constraint follows from the consent state alone — we are never told whether a given call carried tools.

<details><summary>Everything else, and why</summary>

**📄 contractual**

- `human_review` — No request parameter, and no project setting rules it out: Google's terms decide it, on either billing tier. Google's abuse-monitoring page, which covers the Gemini API and AI Studio, says prompts, contextual information and output are kept 55 days to detect violations of the Prohibited Use Policy, that authorized Google employees may assess what its safety filters and abuse detection flag, and that a project showing consistently suspicious activity may be flagged for manual review. Its zero-retention page names no Developer API control for that logging and points to Vertex AI for guaranteed zero retention. The Unpaid Services terms add routine review: human reviewers may read, annotate and process API input and output to improve Google's products, once it is disconnected from the Google Account, API key and Cloud project — on unpaid quota, and on any dataset a billed project chooses to share with Google from its Gemini API logs. Unpaid quota gets the Paid Services data-use terms for a developer in the EEA, Switzerland or the UK, and an app offered to people there may use only Paid Services. A billed project that shares no dataset removes routine review — the lever the training options route on — and never the review of flagged content, so no project honours a refusal and there is no posture to route one to. Checked on 2026-10-12 against Google's Gemini API Additional Terms and its abuse-monitoring, zero-retention and data-logging pages.

**⚙️ account-level**

- `training.foundation` — No API-level opt-out. Paid Services: prompts, cached content, files and responses are not used to improve Google products. Unpaid Services: they are, with possible human review — except for a developer in the EEA, Switzerland or the UK, who gets the Paid Services data-use terms on unpaid quota too. The lever is the billing tier, which the developer chooses; any app offered to users in the EEA, Switzerland or the UK may use only Paid Services, so for those users there is no unpaid project to route to. Elsewhere, it can still be honoured per user IF the developer provisions one project per posture — the SDK then routes each call to the one matching that user's answer. See `credential_routing`.
- `training.personalisation` — Same tier dependency. Can still be honoured per user IF the developer provisions one project per posture — the SDK then routes each call to the one matching that user's answer. See `credential_routing`.
- `training.analytics` — Same tier dependency. Can still be honoured per user IF the developer provisions one project per posture — the SDK then routes each call to the one matching that user's answer. See `credential_routing`.
- `retention.cache` — Implicit in-memory caching (24h, per project) cannot be disabled per request; on Vertex it is a project-level cacheConfig — that is what makes this account-level. The EXPLICIT cache is a different thing and this line used to obscure it: `caches.create` takes `ttl`, `expire_time` and even `kms_key_name` per call, so a developer who creates caches decides their lifetime. That endpoint is not wrapped, and `cached_content` on a wrapped call points AT such a cache rather than configuring one, so there is nothing to force here. Avoid `cached_content` entirely for a zero footprint. Can still be honoured per user IF the developer provisions one project per posture — the SDK then routes each call to the one matching that user's answer. See `credential_routing`.

**➖ absent**

- `memory` — A Gemini app feature, not an API one. `absent` for that reason: the API has no memory for a refusal to switch off.

**❌ unsupported**

- `data_residency` — Not available on the Developer API. Vertex AI offers regional endpoints, VPC-SC and CMEK — a different product this descriptor does not wrap.
- `retention.conversation` — VERIFIED: `generate_content()` accepts only model/contents/config and the REST API rejects unknown fields with HTTP 400, so nothing can be forced on the endpoints wrapped here. That is not the whole picture and the difference matters: `client.interactions` EXISTS in the pinned `google-genai` 2.11.0, its `create` takes a `store` that defaults to TRUE, and it keeps the Interaction object 55 days on a paid tier. A developer migrating to the Interactions API creates 55 days of retention by changing nothing else. So this line means 'the SDK does not reach it yet', not 'Google offers nothing' — the endpoint is refused by the transport guard meanwhile, and adding it as a call site is a backlog card, blocked on the ACT token's route into a client whose adapter nests headers in `config.http_options` while `interactions.create` has no `config` at all. Also: never configure SessionResumptionConfig on the Live API, it retains conversational state for 24h — `client.live` is absent from 2.11.0, so that is a warning for a later client, not a live risk.
- `location` — Not exposed as a request parameter.
- `retention.files` — No per-request file expiry control on the endpoints this descriptor wraps.

</details>

### Mistral

- **Client**: `mistralai`
- **Consent parameters injected into**: kwargs
- **Never overridden** (protected): `model`, `messages`, `prompt`, `agent_id`, `stream`, `http_headers`
- **Verified**: 2026-07-26

**Endpoints wrapped, and what can be forced on each**

| Endpoint | Can force |
| --- | --- |
| `chat.complete` | — nothing |
| `chat.stream` | — nothing |
| `fim.complete` | — nothing |
| `fim.stream` | — nothing |
| `agents.complete` | — nothing |
| `agents.stream` | — nothing |
| `beta.conversations.start` | `store` |

Two endpoints of one provider do not accept the same parameters, so the allowlist is per endpoint. The SDK tells the cloud which one it is about to call, and the cloud decides accordingly — forcing a parameter where it does not exist would break the call for exactly the users who opted out, and for nobody else.

**Technically enforced (2/11)**

- **`agentic_tools`** via the SDK refuses the tool
  - ENFORCED by the SDK, not by a parameter. Every zero-retention arrangement excludes agentic features while the API itself does not block them: a call that hands the model tools falls outside the retention regime the user was promised, and the SDK is the only component that can see it happening, because it is the only one that sees the outgoing request. When the user has refused, the cloud returns a `tools` constraint and the SDK refuses the call (`ToolUseRefusedError`) or strips the tools, per the constraint. Refusing is the default: an agent stripped of its tools does not fail, it answers wrongly, and the developer has no way to know. Note what is NOT sent to us to make this work: nothing. The constraint follows from the consent state alone — we are never told whether a given call carried tools.
- **`retention.conversation`** via `store`
  - `store` on `beta.conversations.start`
    - VERIFIED by request: `store: false` IS accepted on /v1/conversations (200, while an unknown field gives 422), and confirmed a second time offline against the pinned client's signature. It applies to that endpoint ALONE: `chat.complete` is stateless and has no such parameter, which is why this is declared per call site and not on the provider.
  - One parameter, one endpoint. A developer using only `chat.complete` gets nothing from this line, and the per-site allowlist is what says so — the cloud is told which endpoint is about to be called and answers for that one. Conversations the SDK does not wrap (`append`, `restart`, `start_stream`) are refused by the transport guard rather than silently enforced.

<details><summary>Everything else, and why</summary>

**📄 contractual**

- `human_review` — No request parameter, and no console setting for it: the Admin panel's Privacy settings hold the training toggle ("Anonymous improvement data") and, once Mistral has approved it, Zero Data Retention. What Mistral writes does not read like OpenAI's, Anthropic's or Google's. Its Data Processing Addendum describes abuse monitoring on the APIs as automated moderation, which stops once Zero Data Retention is active, and its Privacy Policy keeps API input and output 30 rolling days to monitor abuse unless it is; the only human access its help centre describes is to content a customer reports to support, in an article that names no API; and its Commercial Terms license customer data for "debugging, assessing, reviewing, and correcting the performance" of the products without saying by whom. No document says Mistral staff review API traffic, and none rules it out: the terms decide. Zero Data Retention ends abuse-monitoring retention but is no posture to route a refusal on: it is granted at Mistral's discretion, on pay-as-you-go only, it covers stateless endpoints and not the agents and conversations this descriptor also wraps, and nothing written says what it changes for human review. Checked on 2026-10-12 against Mistral's Commercial Terms of Service, Data Processing Addendum, Privacy Policy and help-centre articles on Zero Data Retention, training opt-out and who can view conversations.

**⚙️ account-level**

- `training.foundation` — Console toggle 'Data usage for improving our services'. Scale is opted out by default; Experiment users must switch off 'Anonymous improvement data' themselves. WARNING: enabling Labs models allows training regardless of plan and of every opt-out. Can still be honoured per user IF the developer provisions one organisation per posture — the SDK then routes each call to the one matching that user's answer. See `credential_routing`.
- `training.personalisation` — Same console setting. Can still be honoured per user IF the developer provisions one organisation per posture — the SDK then routes each call to the one matching that user's answer. See `credential_routing`.
- `training.analytics` — Same console setting. Can still be honoured per user IF the developer provisions one organisation per posture — the SDK then routes each call to the one matching that user's answer. See `credential_routing`.
- `data_residency` — Chosen by ENDPOINT, not by a request parameter. Mistral hosts data in the EU by default, but where inference runs follows the base URL: `api.eu.mistral.ai` (EU and EFTA data centres) and `api.us.mistral.ai` process inference inside their region, at a 10% upcharge, while the global `api.mistral.ai`, which a client uses when it names no region, commits to no inference location. A client names a region with `server_url`, or with `server` set to `eu` or `us` from `mistralai` 2.7.0, as in the releases this repository tests against (Python 3.7.0, Node 3.5.0, both mapping `eu` to `https://api.eu.mistral.ai`); before `mistralai` 2.7.0, `server` set to `eu` still names the global endpoint. A regional endpoint does not make the control plane regional (keys, billing and usage metadata may be handled elsewhere), serves only the models hosted in its region, supports function calling as its only tool and offers no Agents, Batch or Files API; a workload that relies on one of those loses the regional guarantee. Can still be honoured per user IF the developer provisions one client per posture, each built on the endpoint it requires — the SDK then routes each call to the one matching that user's answer. See `credential_routing`. Checked on 2026-10-12 against Mistral's regional-inference documentation and the `mistralai` releases.

**➖ absent**

- `memory` — Not an API capability. `absent` for that reason: the API has no memory for a refusal to switch off.

**❌ unsupported**

- `retention.cache` — `prompt_cache_key` is a routing hint, not a retention control. `safe_prompt` and `guardrails` are moderation, not privacy — do not confuse them.
- `location` — Not exposed as a request parameter.
- `retention.files` — No per-request file expiry control on the endpoints this descriptor wraps.

</details>

### Openai

- **Client**: `openai`
- **Consent parameters injected into**: kwargs
- **Never overridden** (protected): `model`, `messages`, `stream`
- **Verified**: 2026-07-26

**Endpoints wrapped, and what can be forced on each**

| Endpoint | Can force |
| --- | --- |
| `chat.completions.create` | `prompt_cache_retention`, `store` |
| `responses.create` | `background`, `prompt_cache_retention`, `store` |

Two endpoints of one provider do not accept the same parameters, so the allowlist is per endpoint. The SDK tells the cloud which one it is about to call, and the cloud decides accordingly — forcing a parameter where it does not exist would break the call for exactly the users who opted out, and for nobody else.

**Technically enforced (3/11)**

- **`agentic_tools`** via the SDK refuses the tool
  - ENFORCED by the SDK, not by a parameter. Every zero-retention arrangement excludes agentic features while the API itself does not block them: a call that hands the model tools falls outside the retention regime the user was promised, and the SDK is the only component that can see it happening, because it is the only one that sees the outgoing request. When the user has refused, the cloud returns a `tools` constraint and the SDK refuses the call (`ToolUseRefusedError`) or strips the tools, per the constraint. Refusing is the default: an agent stripped of its tools does not fail, it answers wrongly, and the developer has no way to know. Note what is NOT sent to us to make this work: nothing. The constraint follows from the consent state alone — we are never told whether a given call carried tools.
- **`retention.conversation`** via `store`, `background`
  - `store` on `chat.completions.create`, `responses.create`
    - VERIFIED live on both endpoints: `store: false` is accepted, and Responses keeps 30 days of application state otherwise. Under an org-level ZDR arrangement `store` is treated as false whatever the request asks — the account wins, in the safe direction.
  - `background` on `responses.create`
    - Only when: `false` only; `background: true` is additionally rejected outright in the EU region
    - Unlocked by per-call-site parameters. Background mode persists the response for about ten minutes so the client can poll for it — a retention the user did not agree to, created by a flag the developer set for their own convenience. Forcing it off degrades that call to synchronous rather than failing it, which is the right way to lose the argument.
  - Two parameters, one refusal: durable application state (`store`) and the polling window (`background`). Stateful chaining — `conversation` and `previous_response_id` — is the remaining gap: it is not zero-retention eligible, but refusing it means REMOVING an argument the developer passed rather than forcing one, which is the tool-refusal mechanism, not this.
- **`retention.cache`** via `prompt_cache_retention`
  - `prompt_cache_retention` on `chat.completions.create`, `responses.create`
    - Only when: values `in_memory` or `24h`; on GPT-5.5+ only `24h` is supported, so the REFUSAL cannot be honoured from that model onward
    - Forced ONLY on models matching `gpt-4*|gpt-5|gpt-5-*|gpt-5.1|gpt-5.1-*|gpt-5.2|gpt-5.2-*|gpt-5.3|gpt-5.3-*|gpt-5.4|gpt-5.4-*|o1*|o3*|o4*` — anywhere else, including any model released after this list was written, the call goes out unforced and this option is not enforced there
    - Written as `models.only` rather than `models.except` deliberately. "5.5 and later" is open-ended and a glob cannot say it, so a denylist would have to name models OpenAI has not shipped: `gpt-5.7` would match none of them, get `in_memory` forced, and 400 — for exactly the users who refused the cache and for nobody else. An allowlist that falls behind instead stops forcing on a model that would have accepted it: under-protected, still working, and said so in the published matrix.

      `gpt-5|gpt-5-*` and NOT `gpt-5*`, which is the whole difficulty in one line. `gpt-5*` would sweep in `gpt-5.5-mini`, the family this list exists to exclude. The hyphen cannot match the dot, so the two alternatives cover `gpt-5` itself and its `-mini` / `-nano` / dated variants while leaving every `5.x` above 5.4 out. Written as bare `gpt-5` on 2026-07-27, which silently dropped enforcement for `gpt-5-mini` and `gpt-5-nano` — under-protection rather than a broken call, and invisible without reading the glob character by character. Each of `gpt-5.1` to `gpt-5.4` is spelled the same way and for the same reason: `gpt-5.1*` would also match `gpt-5.10` and `gpt-5.11`, which are "5.5 and later" and would take `in_memory` and 400 — the unsurvivable direction, and the one an allowlist is chosen to avoid.

      `gpt-4*`, `o1*`, `o3*` and `o4*` carry over the claim the previous DENYLIST made implicitly: it excluded only 5.5+, so it asserted the parameter worked everywhere else. Keeping them is therefore not a new assertion. It is still weaker than the schema's word "VERIFIED" deserves — none of those families has been exercised with a live call — and settling it is what `tests/live/` is for.
  - VERIFIED against the pinned client: `prompt_cache_retention` is the parameter this SDK can send today. It is deprecated in favour of `prompt_cache_options.ttl` (GPT-5.6+), which `openai` exposes since the 3.x Python and 7.x Node libraries. The two are independent — `prompt_cache_retention` caps the retention, `ttl` sets a minimum lifetime (`30m` or `12h`) — so forcing `in_memory` does not stop a `ttl` of `12h`, and nothing here can force a nested `ttl` yet. Until it can, the SDKs refuse `prompt_cache_options` on an enforced call rather than let a refusal of this option be recorded beside a twelve-hour cache. Orgs without zero-retention default to `24h`, orgs with it default to `in_memory`.

<details><summary>Everything else, and why</summary>

**📄 contractual**

- `training.foundation` — API data has not been used for training since March 2023 unless the org opts in. No request parameter asserts it.
- `training.analytics` — Same data-usage policy, same absence of a request-level lever.
- `human_review` — No request parameter. Abuse-monitoring logs, which may hold prompts and responses, are kept up to 30 days by default, and OpenAI limits access to stored API data to authorized employees — for engineering support, investigating potential platform abuse and legal compliance — and to specialized third-party contractors, solely to review for abuse and misuse. Zero Data Retention and Modified Abuse Monitoring keep customer content out of those logs, and Zero Data Retention with Private Safety Processing runs its safety review where no human can access the content; all three are granted only once OpenAI has approved the organization, then set for the whole organization or per project (Zero Data Retention with Private Safety Processing per project only), so a project carrying one could be routed to through `OpenAI-Project`. None of them rules human review out, which is why this entry is not routable: an image the CSAM classifier flags is retained for manual review even under Zero Data Retention or Modified Abuse Monitoring, and under Safety Retention OpenAI may, with written notice, make a model ineligible for both and human-review the content its classifiers flag. Private Retention with Private Safety Processing (formerly Eyes Off) keeps content in encrypted logs excluded from human review unless the law requires it, and is no posture either: OpenAI applies it when it makes a model ineligible for Zero Data Retention or Modified Abuse Monitoring, its page speaks of an organization ID being provisioned with it, and the CSAM rule applies to it too. Checked on 2026-10-12 against OpenAI's data-controls and Private Safety Processing guides and its enterprise privacy page.

**⚙️ account-level**

- `data_residency` — Residency is chosen by DOMAIN (eu./gb./ca./au./jp./in./sg./kr./ae./us.api.openai.com), not by a parameter. The SDK can READ the base URL and record which region was used — see P1.10. `background: true` is rejected in the EU region. Can still be honoured per user IF the developer provisions one project per posture — the SDK then routes each call to the one matching that user's answer. See `credential_routing`.

**➖ absent**

- `training.personalisation` — A ChatGPT product feature; the API has no per-user personalisation to switch off. `absent` rather than `unsupported` for that reason: the processing does not happen on the API, so a refusal has nothing to stop.
- `memory` — ChatGPT memory is a product feature. Memory in an API app is built by the developer, not the provider. `absent` for that reason: the provider keeps no memory on the API for a refusal to switch off, and what the developer builds is their own processing.

**❌ unsupported**

- `location` — The API infers no location, but it processes one an application declares: `web_search_options.user_location` on `chat.completions.create` and `user_location` on the `web_search` tool of `responses.create`, approximate location parameters for the search. No request lever refuses it. Web search is a tool, so a user who refuses `agentic_tools` loses it, and the declared location with it, through the tool refusal; refusing `location` alone is not possible, which is why this is `unsupported` and not `absent`.
- `retention.files` — OpenAI DOES offer per-request file retention controls — `expires_after` on /v1/files and on /v1/vector_stores, `output_expires_after` on /v1/batches — and none of them is reachable from the endpoints this descriptor wraps. That is our gap, not a carence of the provider's, and the two reasons must not be blurred: (1) `files.create` takes a binary stream that the request hash cannot canonicalise, which is a real and structural obstacle; (2) it carries no `model`, which /v1/authorize requires — but that is OUR API's constraint, and citing it as a limit of OpenAI's reads as a provider failing where it is us. `batches.create` is the cheapest way in, taking an `input_file_id` string rather than a stream. `vector_stores.create` matters most: an option covering the file wrapper but not the indexed content covers the wrong thing. Wrapping upload endpoints is its own card, not a line in this list.

</details>

---

Catalogue version **2** · 11 options · 5 providers
