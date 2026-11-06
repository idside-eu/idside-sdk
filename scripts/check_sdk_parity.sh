#!/usr/bin/env bash
# Run both SDKs and compare the consent dialog they would render.
#
# A question that appears in one SDK and not the other is a user asked something
# their neighbour was not. Unit tests in each package cannot catch that: only
# executing both and comparing can. Run from the repository root.
set -euo pipefail

status=0

for combo in "openai" "anthropic" "gemini" "mistral" "deepseek" "openai anthropic" "openai deepseek" "openai anthropic gemini mistral deepseek"; do
  py=$(cd packages/python && uv run python -c "
import sys; from idside import decidable_options
print(','.join(o.name for o in decidable_options(sys.argv[1].split())))" "$combo")
  nd=$(cd packages/node && node --experimental-strip-types -e "
import {decidableOptions} from './src/index.ts';
console.log(decidableOptions(process.argv[1].split(' ')).map(o=>o.name).join(','));" "$combo")
  if [ "$py" = "$nd" ]; then echo "  ✅ $combo → $py"; else echo "  ❌ DIVERGENCE $combo"; echo "     py=$py"; echo "     node=$nd"; status=1; fi
done

echo
echo "Credential routing (an account setting the app can make per-user, or not):"
# Same question, three configurations: none provisioned, one credential (no
# choice — everyone gets the app's single posture), two (a real question). The
# answer moves an option between "asked" and "disclosed", so the two SDKs
# disagreeing here is the same user-visible divergence as above.
for routed in '{}' '{"mistral": ["default"]}' '{"mistral": ["default", "no_training"]}'; do
  py=$(cd packages/python && uv run python -c "
import json, sys
from idside import decidable_options
print(','.join(
    f'{o.name}[{\"+\".join(o.routed_by)}]'
    for o in decidable_options(['mistral', 'openai'], json.loads(sys.argv[1]))
))" "$routed")
  nd=$(cd packages/node && node --experimental-strip-types -e "
import {decidableOptions} from './src/index.ts';
console.log(decidableOptions(['mistral','openai'], JSON.parse(process.argv[1])).map(o=>\`\${o.name}[\${o.routedBy.join('+')}]\`).join(','));" "$routed")
  if [ "$py" = "$nd" ]; then echo "  ✅ $routed → $py"; else echo "  ❌ DIVERGENCE $routed"; echo "     py=$py"; echo "     node=$nd"; status=1; fi
done

echo
echo "Disclosures, option by option (kind, by_provider and routable_from cross the SDK boundary):"
# Comparing only the SET of kinds across every provider would pass two SDKs that
# disclose the same option two different ways, as long as each kind shows up
# somewhere. Each option is compared, for each combination, with and without a
# second credential at the providers whose options move with routing — and so is
# the level each provider gives it, which is what tells `absent` from
# `unsupported` under the same `not_offered`. One process per SDK walks every
# combination, as the refusal scenarios below do.
combos='["openai", "anthropic", "gemini", "mistral", "deepseek", "openai anthropic", "gemini mistral", "openai anthropic gemini mistral deepseek"]'
routings='{"one credential": {}, "two credentials": {"gemini": ["default", "billed"], "mistral": ["default", "zdr"], "openai": ["default", "zero_retention"]}}'
py=$(cd packages/python && uv run python -c "
import json, sys
from idside import disclosures

def show(d):
    levels = '+'.join(provider + ':' + level for provider, level in d.by_provider.items())
    return d.option.name + '=' + d.kind + '{' + levels + '}[' + '+'.join(d.routable_from) + ']'

for combo in json.loads(sys.argv[1]):
    for label, routed in json.loads(sys.argv[2]).items():
        print(combo + ' / ' + label + ': ' + ','.join(show(d) for d in disclosures(combo.split(), routed)))
" "$combos" "$routings")
nd=$(cd packages/node && node --experimental-strip-types -e "
import {disclosures} from './src/index.ts';
const show = (d) =>
  d.option.name + '=' + d.kind + '{' +
  Object.entries(d.byProvider).map(([provider, level]) => provider + ':' + level).join('+') +
  '}[' + d.routableFrom.join('+') + ']';
for (const combo of JSON.parse(process.argv[1])) {
  for (const [label, routed] of Object.entries(JSON.parse(process.argv[2]))) {
    console.log(combo + ' / ' + label + ': ' + disclosures(combo.split(' '), routed).map(show).join(','));
  }
}" "$combos" "$routings")
if [ "$py" = "$nd" ]; then
  echo "$py" | cut -d: -f1 | sed 's/^/  ✅ /'
else
  echo "  ❌ DIVERGENCE"
  diff <(echo "$py") <(echo "$nd") | sed 's/^/     /' || true
  status=1
fi

echo
echo "The unavailability threshold (the moment each SDK declares the cloud dead):"
# A timeout on a fail-closed path is not a tuning knob: it is the moment the
# product stops the caller's AI calls. Two SDKs with two numbers is one network
# incident cutting a Python application and sparing its Node equivalent. So the
# number is compared, not documented — and compared as each SDK ARMS it on a
# real authorize call, not as a constant somebody wrote down, because a constant
# nothing reads is exactly how this diverged in the first place.
#
# Each SDK derives its key-set deadline from the same constant, pinned by a unit
# test in each package, so this one line holds both hops.
py=$(cd packages/python && uv run python -c "
from idside._transport import AsyncHttpConsentClient, HttpConsentClient

def deadline(client):
    t = client._client.timeout
    phases = {t.connect, t.read, t.write, t.pool}
    if len(phases) != 1:
        return f'phases differ: connect={t.connect} read={t.read} write={t.write} pool={t.pool}'
    return str(int(phases.pop() * 1000))

blocking, non_blocking = deadline(HttpConsentClient()), deadline(AsyncHttpConsentClient())
print(blocking if blocking == non_blocking else f'sync={blocking} async={non_blocking}')")
nd=$(cd packages/node && node --experimental-strip-types -e "
import {HttpConsentClient} from './src/transport.ts';

// Observe the deadline the client arms, without waiting for it: the real timer
// is set an hour out and cleared on the way back.
const armed = [];
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms) => realSetTimeout(fn, (armed.push(ms), 3_600_000));
const body = {
  decision:'allow', consent_state:{}, params_forced:{}, mode:'test',
  act_token:'act.jwt.token', token_id:'act_01k4f9h2m3', expires_at:'2026-09-12T12:00:00Z',
  source:'widget', legal_basis:'consent', binding:'user_id'
};
const client = new HttpConsentClient('https://cloud.test', {
  fetch: async () => new Response(JSON.stringify(body), {headers: {'content-type': 'application/json'}}),
});
await client.authorize({provider:'openai', model:'gpt-4o-mini', requestHash:'sha256:abc', userId:'usr_marie_456'});
globalThis.setTimeout = realSetTimeout;
console.log(new Set(armed).size === 1 ? String(armed[0]) : 'deadlines differ: ' + armed.join(','));")
if [ "$py" = "$nd" ]; then
  echo "  ✅ both SDKs give up on the cloud after $py ms"
else
  echo "  ❌ DIVERGENCE"; echo "     py=$py ms"; echo "     node=$nd ms"; status=1
fi

echo
echo "How a refusal from the cloud is classified (same class, and where meant, same words):"
# A developer catches the class, so the two SDKs must pick the same one for the
# same answer: a 401 that is the visitor's binding and not the key, a 409 that
# asks for the widget and is not an outage, a 403 whose `reason` names an
# organization's policy hold rather than a suspension, and every answer they do
# not qualify. Where the message is written once for both — the binding and
# refresh remedies, the sentence naming the default address — the words are
# compared too, and so are the URL each request went to and the Authorization
# header it carried. Both SDKs run under the SAME environment, twice: with IDSIDE_BASE_URL
# and IDSIDE_API_KEY unset (the default address, whose failures name the
# missing setting, and no key) and with both set (the configured host and key,
# and no such sentence). Clearing a variable on one side only once let Node's
# transport ignore both unseen. The network is stubbed out: nothing here leaves
# the machine, and the key is a fictitious one.
scenarios='[
  [401, null, false], [401, "https://idside.eu/errors/ubt_invalid", true],
  [401, "https://idside.eu/errors/ubt_required", true],
  [401, "https://idside.eu/errors/ubt_expired", false],
  [403, "https://idside.eu/errors/fraud_suspended", false],
  [403, "https://idside.eu/errors/policy_hold", true],
  [409, "https://idside.eu/errors/consent_refresh_required", true],
  [409, "https://idside.eu/errors/consent_not_collected", true],
  [422, "https://idside.eu/errors/invalid_request", true],
  [404, null, true], [0, null, true]
]'
parity_key="sk_test_parity_key" # pragma: allowlist secret
for configured in "" "https://cloud.parity.test"; do
  if [ -z "$configured" ]; then
    label="IDSIDE_BASE_URL and IDSIDE_API_KEY unset"
    environment=(env -u IDSIDE_BASE_URL -u IDSIDE_API_KEY)
  else
    label="IDSIDE_BASE_URL=$configured and IDSIDE_API_KEY set"
    environment=(env IDSIDE_BASE_URL="$configured" IDSIDE_API_KEY="$parity_key")
  fi
  echo "  with $label:"
  py=$(cd packages/python && "${environment[@]}" uv run python -c "
import json, sys
import httpx
from idside._transport import HttpConsentClient

for status, type_, same in json.loads(sys.argv[1]):
    sent = []
    def answer(request, status=status, type_=type_):
        sent.append(str(request.url))
        sent.append('auth=' + request.headers.get('authorization', 'none').replace(' ', '_'))
        if status == 0:
            raise httpx.ConnectError('refused', request=request)
        body = {'title': 'T', 'detail': 'D.'} | ({'type': type_} if type_ else {})
        return httpx.Response(status, json=body)
    client = HttpConsentClient(transport=httpx.MockTransport(answer))
    try:
        client.authorize(provider='openai', model='gpt-4o', request_hash='sha256:x', user_id='usr_marie_456')
    except Exception as exc:
        print(status, type_, type(exc).__name__, getattr(exc, 'reason', None), sent[0], sent[1], str(exc) if same else '')
" "$scenarios")
  nd=$(cd packages/node && "${environment[@]}" node --experimental-strip-types -e "
import {HttpConsentClient} from './src/transport.ts';

for (const [status, type, same] of JSON.parse(process.argv[1])) {
  const sent = [];
  const client = new HttpConsentClient(undefined, {
    fetch: async (url, init) => {
      sent.push(String(url));
      sent.push('auth=' + (new Headers(init?.headers).get('authorization') ?? 'none').replace(' ', '_'));
      if (status === 0) throw new TypeError('fetch failed');
      const body = {title: 'T', detail: 'D.', ...(type ? {type} : {})};
      return new Response(JSON.stringify(body), {status, headers: {'content-type': 'application/json'}});
    },
  });
  try {
    await client.authorize({provider: 'openai', model: 'gpt-4o', requestHash: 'sha256:x', userId: 'usr_marie_456'});
  } catch (error) {
    console.log(status, type === null ? 'None' : type, error.constructor.name, error.reason ?? 'None', sent[0], sent[1], same ? error.message : '');
  }
}" "$scenarios")
  if [ "$py" = "$nd" ]; then
    echo "$py" | cut -d' ' -f1-6 | sed 's/^/    ✅ /'
  else
    echo "    ❌ DIVERGENCE"
    diff <(echo "$py") <(echo "$nd") | sed 's/^/       /'
    status=1
  fi
done

echo
echo "An identity that is not a string (sent as written, judged by the cloud alone):"
# Node once turned every user_id into text on its way out: `true` was authorized
# as the user "true", and every object as one person, "[object Object]", where
# Python sent the value and the cloud refused it. Only identities that were
# strings were compared here, which is how that went unseen. Each value now goes
# through wrap() in both SDKs, to a stubbed cloud that applies the cloud's rule
# (a non-blank string or a finite number names somebody, anything else is a
# 422); what reached it, what came back and whether the provider was called must
# match. Floats are left out on purpose: JavaScript has no 42.0 to send.
identities='["usr_marie_456", 4242101, true, false, 0, null, {"id": "usr_marie_456"}, ["usr_marie_456"], {}, []]'
allow='{"decision": "allow", "consent_state": {}, "params_forced": {}, "mode": "test",
  "act_token": "act.jwt.token", "token_id": "act_01k4f9h2m3", "expires_at": "2026-09-12T12:00:00Z",
  "source": "widget", "legal_basis": "consent", "binding": "user_id"}'
py=$(cd packages/python && uv run python -c "
import json, sys
from types import SimpleNamespace
import httpx
from idside import consent
from idside._transport import HttpConsentClient
from idside.adapters import ProviderAdapter
from idside.providers import load_descriptors

allow = json.loads(sys.argv[2])
for identity in json.loads(sys.argv[1]):
    sent, calls = [], []
    def answer(request):
        body = json.loads(request.content)
        value = body.get('user_id')
        sent.append(json.dumps(value, separators=(',', ':')) if 'user_id' in body else '-')
        named = (isinstance(value, str) and value.strip() != '') or (
            isinstance(value, (int, float)) and not isinstance(value, bool))
        if named:
            return httpx.Response(200, json=allow)
        return httpx.Response(422, json={'type': 'https://idside.eu/errors/invalid_request', 'title': 'T', 'detail': 'D.'})
    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return {'id': 'chatcmpl_fake'}
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        adapter=ProviderAdapter(load_descriptors()['openai']),
        consent_client=HttpConsentClient(base_url='https://cloud.parity.test', transport=httpx.MockTransport(answer)),
    )
    try:
        client.chat.completions.create(model='gpt-4o-mini', messages=[], user_id=identity)
        outcome = 'allowed'
    except Exception as exc:
        outcome = type(exc).__name__
    print(json.dumps(identity, separators=(',', ':')), 'sent=' + (sent[0] if sent else 'nothing'), outcome, f'provider={len(calls)}')
" "$identities" "$allow")
nd=$(cd packages/node && node --experimental-strip-types -e "
import {wrap} from './src/consent.ts';
import {GenericAdapter} from './src/adapters/index.ts';
import {loadDescriptors} from './src/descriptors.ts';
import {HttpConsentClient} from './src/transport.ts';

const allow = process.argv[2];
for (const identity of JSON.parse(process.argv[1])) {
  const sent = [], calls = [];
  const cloud = new HttpConsentClient('https://cloud.parity.test', {
    fetch: async (url, init) => {
      const body = JSON.parse(init.body);
      const value = body.user_id;
      sent.push('user_id' in body ? JSON.stringify(value) : '-');
      const named = (typeof value === 'string' && value.trim() !== '') || (typeof value === 'number' && Number.isFinite(value));
      return named
        ? new Response(allow, {status: 200, headers: {'content-type': 'application/json'}})
        : new Response(JSON.stringify({type: 'https://idside.eu/errors/invalid_request', title: 'T', detail: 'D.'}), {status: 422});
    },
  });
  const client = wrap(
    {chat: {completions: {create: async (body) => { calls.push(body); return {id: 'chatcmpl_fake'}; }}}},
    {adapter: new GenericAdapter(loadDescriptors().get('openai')), consentClient: cloud},
  );
  let outcome;
  try {
    await client.chat.completions.create({model: 'gpt-4o-mini', messages: [], user_id: identity});
    outcome = 'allowed';
  } catch (error) {
    outcome = error.constructor.name;
  }
  console.log(JSON.stringify(identity), 'sent=' + (sent[0] ?? 'nothing'), outcome, 'provider=' + calls.length);
}" "$identities" "$allow")
if [ "$py" = "$nd" ]; then
  echo "$py" | sed 's/^/  ✅ /'
else
  echo "  ❌ DIVERGENCE"
  diff <(echo "$py") <(echo "$nd") | sed 's/^/     /'
  status=1
fi

echo
echo "The agentic tools refusal (one message, the same words, whoever refused):"
# The SDK cannot know whether the person, nobody asking them, or an
# organization's policy refused (invariant #1), so the message must be true in
# all three — and identical, since a developer searches for it. Each scenario
# is a provider, a call site (empty: none named, so every site's fields are
# read), the call's arguments and the cloud's instruction. Mistral and Gemini
# pass only `tools`: their Node libraries spell every other field in camelCase
# (`agentId`, `cachedContent`), so a message naming one differs by that
# library's own spelling and nothing else.
refusals='[
  ["openai", "chat.completions.create", {"tools": [{"type": "function"}], "functions": [{"name": "f"}]}, "refuse"],
  ["openai", "chat.completions.create", {"tools": [{"type": "function"}], "web_search_options": {}}, "strip"],
  ["openai", "responses.create", {"tools": [{"type": "function"}], "conversation": "conv_1", "previous_response_id": "resp_1"}, "strip"],
  ["openai", "", {"web_search_options": {}, "prompt": {"id": "pmpt_1"}}, "refuse"],
  ["anthropic", "messages.create", {"mcp_servers": [{"name": "m"}]}, "refuse"],
  ["mistral", "chat.complete", {"tools": [{"type": "function"}]}, "refuse"],
  ["gemini", "models.generate_content", {"config": {"tools": [{"function_declarations": []}]}}, "refuse"]
]'
py=$(cd packages/python && uv run python -c "
import json, sys
from idside.adapters import GeminiAdapter, ProviderAdapter
from idside.providers import load_descriptors

for provider, call_site, params, action in json.loads(sys.argv[1]):
    descriptor = load_descriptors()[provider]
    adapter = (GeminiAdapter if provider == 'gemini' else ProviderAdapter)(descriptor)
    try:
        adapter.apply_constraints({'model': 'm', **params}, {'tools': action}, call_site or None)
        print(provider, call_site or '-', action, 'passed')
    except Exception as exc:
        print(provider, call_site or '-', action, type(exc).__name__, str(exc))
" "$refusals")
nd=$(cd packages/node && node --experimental-strip-types -e "
import {GeminiAdapter, GenericAdapter} from './src/adapters/index.ts';
import {loadDescriptors} from './src/descriptors.ts';

for (const [provider, callSite, params, action] of JSON.parse(process.argv[1])) {
  const descriptor = loadDescriptors().get(provider);
  const adapter = provider === 'gemini' ? new GeminiAdapter(descriptor) : new GenericAdapter(descriptor);
  try {
    adapter.applyConstraints({model: 'm', ...params}, {tools: action}, callSite || undefined);
    console.log(provider, callSite || '-', action, 'passed');
  } catch (error) {
    console.log(provider, callSite || '-', action, error.constructor.name, error.message);
  }
}" "$refusals")
if [ "$py" = "$nd" ]; then
  echo "$py" | cut -d' ' -f1-4 | sed 's/^/  ✅ /'
else
  echo "  ❌ DIVERGENCE"
  diff <(echo "$py") <(echo "$nd") | sed 's/^/     /'
  status=1
fi

echo
echo "The journal (a developer debugging one SDK must read the same story in the other):"
# Each SDK journals through its own ecosystem's channel — Python through
# `logging` behind a NullHandler, Node through `util.debuglog` — because a
# library that invents a channel is a library nobody can configure. So the pipe
# differs on purpose and the CONTENT must not: same events, same fields, same
# order, same rendering. Unit tests in each package cannot see that; only
# running both on one scenario and diffing can.
#
# Two values are masked because they are meant to differ: the duration, and the
# subject pseudonym, whose salt is random per process precisely so that two
# processes cannot produce the same one. Everything else — including the
# request_hash, which crosses to the cloud — must match character for character.
mask() { sed -E 's/duration_ms=[0-9]+/duration_ms=N/; s/subject=[0-9a-f]+/subject=S/'; }

py=$(cd packages/python && uv run python -c "
import logging, sys
from types import SimpleNamespace
from idside import consent
from idside._transport import ConsentDecision
from idside.adapters import ProviderAdapter
from idside.providers import load_descriptors

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter('%(message)s'))
log = logging.getLogger('idside')
log.addHandler(handler)
log.setLevel(logging.DEBUG)

decision = ConsentDecision(
    consent_state={'retention.conversation': False},
    params_forced={'store': False},
    act_token='act.jwt.token',
    source='widget',
    legal_basis='consent',
    binding='user_id',
)

class Cloud:
    async def authorize(self, **kwargs): return decision

class Completions:
    async def create(self, **kwargs): return {'id': 'chatcmpl_fake'}

# The async client, because Node has no other kind: comparing its journal
# against Python's blocking one would compare two different scenarios.
client = consent.wrap(
    SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    adapter=ProviderAdapter(load_descriptors()['openai']),
    consent_client=Cloud(),
)
import asyncio
asyncio.run(client.chat.completions.create(
    model='gpt-4o-mini',
    messages=[{'role': 'user', 'content': 'hello'}],
    user_id='usr_marie_456',
))" | mask)

# `util.debuglog` writes to stderr, so the journal is read from there and stdout
# is discarded. Node writes its own warnings to the same stream, though, and on
# the type-stripping floor this job pins it emits one — so the filter below is
# an ALLOWLIST: a line counts as journal only if it carries the `IDSIDE <pid>:`
# prefix the runtime puts there. Dropping known warnings instead would have made
# this pass until the next Node release invented a new one.
nd=$(cd packages/node && NODE_DEBUG=idside node --experimental-strip-types -e "
import {wrap} from './src/consent.ts';
import {GenericAdapter} from './src/adapters/index.ts';
import {loadDescriptors} from './src/descriptors.ts';

const decision = {
  consentState: {'retention.conversation': false},
  paramsForced: {store: false},
  actToken: 'act.jwt.token',
  source: 'widget',
  legalBasis: 'consent',
  binding: 'user_id',
  constraints: {},
};
const cloud = {authorize: async () => decision};
const calls = [];
const client = wrap(
  {chat: {completions: {create: async (body) => { calls.push(body); return {id: 'chatcmpl_fake'}; }}}},
  {adapter: new GenericAdapter(loadDescriptors().get('openai')), consentClient: cloud},
);
await client.chat.completions.create({
  model: 'gpt-4o-mini',
  messages: [{role: 'user', content: 'hello'}],
  user_id: 'usr_marie_456',
});" 2>&1 >/dev/null | sed -n -E 's/^IDSIDE [0-9]+: //p' | mask)

if [ "$py" = "$nd" ]; then
  echo "$py" | sed 's/^/  ✅ /'
else
  echo "  ❌ DIVERGENCE"
  diff <(echo "$py") <(echo "$nd") | sed 's/^/     /'
  status=1
fi

exit "$status"
