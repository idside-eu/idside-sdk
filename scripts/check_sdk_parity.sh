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
echo "An identity that is not a string, or none at all (sent as written, judged by the cloud alone):"
# Node once turned every user_id into text on its way out: `true` was authorized
# as the user "true", and every object as one person, "[object Object]", where
# Python sent the value and the cloud refused it. Only identities that were
# strings were compared here, which is how that went unseen. Each value now goes
# through wrap() in both SDKs, to a stubbed cloud that applies the cloud's rule
# (a non-blank string or a finite number names somebody, anything else is a
# 422, and a body with no identity at all is authorized with the API key as its
# subject); what reached it, what came back and whether the provider was called
# must match. Floats are left out on purpose: JavaScript has no 42.0 to send.
#
# Naming the end user is optional, and the two SDKs must agree on what "nobody"
# is: the argument left out ("absent" below), Python's None and JavaScript's
# null. A value that is there and names nobody ("", 0, false, [], {}) is refused
# locally in both, before any request.
identities='["absent", "usr_marie_456", 4242101, true, false, 0, "", null, {"id": "usr_marie_456"}, ["usr_marie_456"], {}, []]'
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
        if request.url.path == '/v1/content-policy':
            return httpx.Response(200, json={'enabled': False})  # cleaning off
        body = json.loads(request.content)
        value = body.get('user_id')
        sent.append(json.dumps(value, separators=(',', ':')) if 'user_id' in body else '-')
        if 'user_id' not in body and 'ubt' not in body:
            return httpx.Response(200, json={**allow, 'source': 'default_policy', 'binding': 'api_key'})
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
    named = {} if identity == 'absent' else {'user_id': identity}
    try:
        client.chat.completions.create(model='gpt-4o-mini', messages=[], **named)
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
      if (new URL(url).pathname === '/v1/content-policy') {
        return new Response(JSON.stringify({enabled: false}), {status: 200}); // cleaning off
      }
      const body = JSON.parse(init.body);
      const value = body.user_id;
      sent.push('user_id' in body ? JSON.stringify(value) : '-');
      if (!('user_id' in body) && !('ubt' in body)) {
        const unnamed = {...JSON.parse(allow), source: 'default_policy', binding: 'api_key'};
        return new Response(JSON.stringify(unnamed), {status: 200, headers: {'content-type': 'application/json'}});
      }
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
    await client.chat.completions.create({model: 'gpt-4o-mini', messages: [], ...(identity === 'absent' ? {} : {user_id: identity})});
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
echo "An identity argument spelled another way (refused by both, whatever its value, in the same words):"
# A call may name nobody, so \`userId\` for \`user_id\` was authorized in Node as a
# call about the API key, the misspelled field sent to the provider in the body,
# while Python refused it with a message that did not say what was wrong — or,
# given None, authorized it and failed in the provider's own signature. Both now
# refuse any key that matches an identity argument once case and separators are
# taken out, before any I/O. The message is compared word for word; the last
# case is OpenAI's own \`user\` field, which must still go out.
lookalikes='[{"userId": "team-marketing"}, {"userId": null}, {"UserId": "team-marketing"}, {"user-id": "team-marketing"}, {"USER_ID": "team-marketing"}, {"user id": "team-marketing"}, {"Ubt": "eyJ.ubt.sig"}, {"user_id": "usr_marie_456", "userId": "usr_marie_456"}, {"user_id": "", "userId": "team-marketing"}, {"user": "end-user-hash"}]'
py=$(cd packages/python && uv run python -c "
import json, sys
from types import SimpleNamespace
from idside import consent
from idside._transport import ConsentDecision
from idside.adapters import ProviderAdapter
from idside.providers import load_descriptors

for case in json.loads(sys.argv[1]):
    asked, calls = [], []
    class Cloud:
        def authorize(self, **kwargs):
            asked.append(kwargs)
            return ConsentDecision(consent_state={}, params_forced={}, act_token='act.jwt.token',
                source='default_policy', legal_basis='legitimate_interest', binding='api_key')
    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return {'id': 'chatcmpl_fake'}
    client = consent.wrap(
        SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        adapter=ProviderAdapter(load_descriptors()['openai']),
        consent_client=Cloud(),
    )
    try:
        client.chat.completions.create(model='gpt-4o-mini', messages=[], **case)
        outcome = 'allowed'
    except Exception as exc:
        outcome = type(exc).__name__ + ': ' + str(exc)
    print(json.dumps(case, separators=(',', ':')), f'cloud={len(asked)} provider={len(calls)}', outcome)
" "$lookalikes")
nd=$(cd packages/node && node --experimental-strip-types -e "
import {wrap} from './src/consent.ts';
import {GenericAdapter} from './src/adapters/index.ts';
import {loadDescriptors} from './src/descriptors.ts';

for (const kase of JSON.parse(process.argv[1])) {
  const asked = [], calls = [];
  const cloud = {authorize: async (request) => (asked.push(request), {consentState: {}, paramsForced: {}, actToken: 'act.jwt.token',
    source: 'default_policy', legalBasis: 'legitimate_interest', binding: 'api_key', constraints: {}})};
  const client = wrap(
    {chat: {completions: {create: async (body) => { calls.push(body); return {id: 'chatcmpl_fake'}; }}}},
    {adapter: new GenericAdapter(loadDescriptors().get('openai')), consentClient: cloud},
  );
  let outcome;
  try {
    await client.chat.completions.create({model: 'gpt-4o-mini', messages: [], ...kase});
    outcome = 'allowed';
  } catch (error) {
    outcome = error.constructor.name + ': ' + error.message;
  }
  console.log(JSON.stringify(kase), 'cloud=' + asked.length + ' provider=' + calls.length, outcome);
}" "$lookalikes")
if [ "$py" = "$nd" ]; then
  echo "$py" | cut -c1-150 | sed 's/^/  ✅ /'
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
#
# Two calls: one naming its user, and one naming nobody, whose line must carry
# `binding=api_key` and no `subject=` at all in both SDKs.
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

key_decision = ConsentDecision(
    consent_state={'retention.conversation': False},
    params_forced={'store': False},
    act_token='act.jwt.token',
    source='default_policy',
    legal_basis='legitimate_interest',
    binding='api_key',
)

class Cloud:
    async def authorize(self, **kwargs):
        return decision if kwargs['user_id'] or kwargs['ubt'] else key_decision

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
))
asyncio.run(client.chat.completions.create(
    model='gpt-4o-mini',
    messages=[{'role': 'user', 'content': 'hello'}],
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
const keyDecision = {...decision, source: 'default_policy', legalBasis: 'legitimate_interest', binding: 'api_key'};
const cloud = {authorize: async (request) => (request.userId === undefined && request.ubt === undefined ? keyDecision : decision)};
const calls = [];
const client = wrap(
  {chat: {completions: {create: async (body) => { calls.push(body); return {id: 'chatcmpl_fake'}; }}}},
  {adapter: new GenericAdapter(loadDescriptors().get('openai')), consentClient: cloud},
);
await client.chat.completions.create({
  model: 'gpt-4o-mini',
  messages: [{role: 'user', content: 'hello'}],
  user_id: 'usr_marie_456',
});
await client.chat.completions.create({
  model: 'gpt-4o-mini',
  messages: [{role: 'user', content: 'hello'}],
});" 2>&1 >/dev/null | sed -n -E 's/^IDSIDE [0-9]+: //p' | mask)

if [ "$py" = "$nd" ]; then
  echo "$py" | sed 's/^/  ✅ /'
else
  echo "  ❌ DIVERGENCE"
  diff <(echo "$py") <(echo "$nd") | sed 's/^/     /'
  status=1
fi

echo
echo "Prompt cleaning (both engines on the same seeded texts, and the digests they declare):"
# The shared vectors pin what each test suite expects; this runs both engines
# side by side on text neither suite has seen — seeded token soup mixing every
# detector's shapes, separators, accents, invisible and astral characters —
# under the reference policy and under custom entries, and diffs every output,
# count and block, each written in RFC 8785 form (one canonicaliser, the one the
# request hash uses, so the two lines can only differ if the results do). Then
# the digests that cross to the cloud: the rules file and each policy's digest.
corpus=$(mktemp)
trap 'rm -f "$corpus"' EXIT
python3 -c '
import json, random
rng = random.Random(20261116)
tokens = ["a", "Z", "1", "0", "@", ".", "-", ":", "/", "+", " ", "[", "]", "_", "=",
          "\x27", "\"", "\n", "é", "​", " ", "ﬁ", "\U0001f600", "–",
          "sk-", "sk_" + "live_", "-----BEGIN " + "PRIVATE KEY-----", "x@y.zz",
          "4111111111111111", "4111 ", "FR76", "3000 ", "06 12 34 56 78", "+33", "siren",
          "123 456 782", "https://", "postgres://u:p@h", "Bearer ", "password=", "eyJ",
          "[EMAIL_1]", "10.0.0.1", "::", "AB-123-CD", "AKIA", "mistral", "bic", "TESTDEFF",
          "acme.example", "projet atlas", "PRJ-1234", "fe80::1", "00:1a:2b:3c:4d:5e",
          "version ", "v", "4.2.1.3", "\\n", "> ", "-----END " + "PRIVATE KEY-----",
          "Authorization: ", "Basic ", "(+33) ", "06/12/", "  ", "\u20ac", "\u043c@\u0440\u0444",
          "\u044f", "git@github.com:", "@2x.png", "xapp-1-", "glffct-", "PGPASS" + "WORD=",
          "Password=\x27", "os.environ", "mot de passe : ", "_KEY=", "R.C.S.", "FR 76-"]
print(json.dumps(["".join(rng.choice(tokens) for _ in range(rng.randint(1, 30))) for _ in range(1500)]))
' > "$corpus"
policies='[
  {"schema": 1, "detectors": "ALL"},
  {"schema": 1, "detectors": {"email": "mask", "credit_card": "block"}, "attachments": "block",
   "custom": [{"kind": "term", "value": "Projet Atlas", "action": "mask", "label": "PROJECT"},
              {"kind": "domain", "value": "acme.example", "action": "mask", "label": "DOMAIN"},
              {"kind": "pattern", "value": "PRJ-####", "action": "mask", "label": "REF"}]}
]'
py=$(cd packages/python && uv run python -c "
import json, sys
from idside._jcs import canonicalize
from idside._cleaning import CATALOGUE_IDS, clean, content_policy_digest, rules_digest
texts, policies = json.load(open(sys.argv[1], encoding='utf-8')), json.loads(sys.argv[2])
for policy in policies:
    if policy['detectors'] == 'ALL':
        policy['detectors'] = dict.fromkeys(CATALOGUE_IDS, 'mask')
jcs = lambda value: canonicalize(value).decode()
print('rules', rules_digest())
for p, policy in enumerate(policies):
    print('policy', p, content_policy_digest(policy))
    for t, text in enumerate(texts):
        r = clean({'messages': [{'role': 'user', 'content': text}]}, policy, provider='openai')
        print(p, t, jcs(r.payload), jcs(dict(r.counts)), jcs(dict(r.blocked)), r.attachments)
" "$corpus" "$policies")
nd=$(cd packages/node && node --experimental-strip-types -e "
import {readFileSync} from 'node:fs';
import {canonicalizeToString as jcs} from './src/jcs.ts';
import {CATALOGUE_IDS, clean, contentPolicyDigest, rulesDigest} from './src/cleaning/index.ts';
const texts = JSON.parse(readFileSync(process.argv[1], 'utf8'));
const policies = JSON.parse(process.argv[2]);
for (const policy of policies) {
  if (policy.detectors === 'ALL') policy.detectors = Object.fromEntries(CATALOGUE_IDS.map((id) => [id, 'mask']));
}
console.log('rules', rulesDigest());
policies.forEach((policy, p) => {
  console.log('policy', p, contentPolicyDigest(policy));
  texts.forEach((text, t) => {
    const r = clean({messages: [{role: 'user', content: text}]}, policy, 'openai');
    console.log(p, t, jcs(r.payload), jcs(r.counts), jcs(r.blocked), r.attachments);
  });
});" "$corpus" "$policies")
if [ "$py" = "$nd" ]; then
  echo "$py" | grep -E '^(rules|policy) ' | sed 's/^/  ✅ /'
  echo "  ✅ $(echo "$py" | grep -cvE '^(rules|policy) ') cleaned bodies identical, counts and blocks included"
else
  echo "  ❌ DIVERGENCE"
  diff <(echo "$py") <(echo "$nd") | head -20 | sed 's/^/     /'
  status=1
fi

echo
echo "Prompt cleaning on a call (the same journal, the same request, the same refusal):"
# What a developer reads when a call is cleaned or blocked, and what the cloud
# is sent: the journal lines (counts, never values), the authorize request's
# hash and declaration, and the refusal's class, rule ids and message, each
# structure written in RFC 8785 form. The policy is handed over already
# verified, as each SDK's cache holds it, so only the enforcement path is
# compared here; fetching and checking it is held by each package's tests.
# The `wrap` line is left out: Python wraps a blocking client here, Node has
# only the async kind, and the journal section above compares that line.
policy_json='{"schema": 1, "app_id": "app_medhelp", "revision": 7, "detectors": {"email": "mask", "credit_card": "block"}, "attachments": "allow", "custom": [{"kind": "term", "value": "Projet Atlas", "action": "mask", "label": "PROJECT"}]}'
py=$(cd packages/python && uv run python -c "
import json, logging, sys
from types import SimpleNamespace
from idside import consent
from idside._jcs import canonicalize
from idside._cleaning import content_policy_digest
from idside._content_policy import PolicyState
from idside._transport import ConsentDecision
from idside.adapters import ProviderAdapter
from idside.exceptions import ContentBlockedError
from idside.providers import load_descriptors

jcs = lambda value: canonicalize(value).decode()
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter('%(message)s'))
log = logging.getLogger('idside')
log.addHandler(handler)
log.setLevel(logging.DEBUG)
policy = json.loads(sys.argv[1])
digest = content_policy_digest(policy)
state = PolicyState(policy=policy, declaration={'digest': digest, 'engine': '1'},
                    digest=digest, refresh_at=0, valid_until=0)

class Cloud:
    def content_policy(self): return state
    def content_events(self, **report): print('event', jcs(report))
    def authorize(self, **request):
        print('authorize', request['request_hash'], jcs(request['content_policy']))
        return ConsentDecision({}, {}, 'act.jwt.token', 'default_policy', 'legitimate_interest', 'user_id')

class Completions:
    def create(self, **kwargs): print('sent', jcs(kwargs['messages'])); return {}

client = consent.wrap(SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
                      adapter=ProviderAdapter(load_descriptors()['openai']), consent_client=Cloud())
client.chat.completions.create(model='gpt-4o-mini', user_id='usr_marie_456', messages=[
    {'role': 'system', 'content': 'About Projet Atlas.'}, {'role': 'user', 'content': 'Write to marie@example.com'}])
try:
    client.chat.completions.create(model='gpt-4o-mini', user_id='usr_marie_456',
                                   messages=[{'role': 'user', 'content': 'card 4111 1111 1111 1111'}])
except ContentBlockedError as error:
    print(type(error).__name__, jcs(error.blocked), error)
" "$policy_json" | grep -v '^wrap ' | mask)
nd=$(cd packages/node && NODE_DEBUG=idside node --experimental-strip-types -e "
import {wrap} from './src/consent.ts';
import {GenericAdapter} from './src/adapters/index.ts';
import {loadDescriptors} from './src/descriptors.ts';
import {canonicalizeToString as jcs} from './src/jcs.ts';
import {contentPolicyDigest} from './src/cleaning/index.ts';
import {PolicyState} from './src/contentPolicy.ts';
import {ContentBlockedError} from './src/exceptions.ts';

const policy = JSON.parse(process.argv[1]);
const digest = contentPolicyDigest(policy);
const state = new PolicyState(policy, {digest, engine: '1'}, digest, 0, 0);
// Written on the journal's own channel, so the lines interleave as Python's do.
const out = (line) => process.stderr.write('IDSIDE 0: ' + line + String.fromCharCode(10));
const cloud = {
  contentPolicy: async () => state,
  contentEvents: async (report) => out('event ' + jcs(report)),
  authorize: async (request) => {
    out('authorize ' + request.requestHash + ' ' + jcs(request.contentPolicy));
    return {consentState: {}, paramsForced: {}, actToken: 'act.jwt.token', source: 'default_policy',
            legalBasis: 'legitimate_interest', binding: 'user_id', constraints: {}};
  },
};
const client = wrap(
  {chat: {completions: {create: async (body) => { out('sent ' + jcs(body.messages)); return {}; }}}},
  {adapter: new GenericAdapter(loadDescriptors().get('openai')), consentClient: cloud},
);
await client.chat.completions.create({model: 'gpt-4o-mini', user_id: 'usr_marie_456', messages: [
  {role: 'system', content: 'About Projet Atlas.'}, {role: 'user', content: 'Write to marie@example.com'}]});
try {
  await client.chat.completions.create({model: 'gpt-4o-mini', user_id: 'usr_marie_456',
    messages: [{role: 'user', content: 'card 4111 1111 1111 1111'}]});
} catch (error) {
  if (!(error instanceof ContentBlockedError)) throw error;
  out(error.name + ' ' + jcs(error.blocked) + ' ' + error.message);
}" "$policy_json" 2>&1 >/dev/null | sed -n -E 's/^IDSIDE [0-9]+: //p' | grep -v '^wrap ' | mask)
if [ "$py" = "$nd" ]; then
  echo "$py" | sed 's/^/  ✅ /'
else
  echo "  ❌ DIVERGENCE"
  diff <(echo "$py") <(echo "$nd") | sed 's/^/     /'
  status=1
fi

echo
echo "A signed cleaning policy (the same answers, the same verdict, whichever SDK reads them):"
# Each SDK checks the policy before it applies anything: key purpose, signature
# (high s accepted, as ECDSA does), type, algorithm, issuer, digest, application
# and, last, dates — a policy out of step with this machine's clock is told in
# the same words by both, compared below. A case one accepts and the other refuses is a call cleaned in one
# language and stopped, or sent uncleaned, in the other. The answers are signed
# here, by Python's test dependency, with keys that live only in that process's
# memory; only tokens and public keys reach the file both SDKs then read.
cases_file="$(mktemp)"
# One trap per signal: this one replaces the corpus's, so it removes both.
trap 'rm -f "$corpus" "$cases_file"' EXIT
(cd packages/python && uv run python - "$cases_file" <<'SIGN'
import base64, json, sys
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from idside._cleaning import content_policy_digest

NOW, N = 1_800_000_000, 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
b64 = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode()
keys = {kid: ec.generate_private_key(ec.SECP256R1()) for kid in ("content-policy-g1", "act-g1")}
def jwk(kid, key):
    numbers = key.public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256", "kid": kid, "use": "sig", "alg": "ES256",
            "x": b64(numbers.x.to_bytes(32, "big")), "y": b64(numbers.y.to_bytes(32, "big"))}
jwks = {"keys": [jwk(kid, key) for kid, key in keys.items()]}
policy = {"schema": 1, "app_id": "app_medhelp", "revision": 7, "detectors": {"email": "mask"},
          "attachments": "allow", "custom": [{"kind": "term", "value": "Projet Atlas", "action": "mask", "label": "PROJECT"}]}
def claims(**overrides):
    return {"iss": "idside.eu", "app_id": "app_medhelp", "iat": NOW - 10, "exp": NOW + 890,
            "digest": content_policy_digest(policy), "policy": policy, **overrides}
def token(claims_, kid="content-policy-g1", signer=None, header=None, high_s=False, flip=False, raw_claims=None,
          shape=None):
    head = header or {"alg": "ES256", "typ": "content-policy+jwt", "kid": kid}
    body = raw_claims if raw_claims is not None else json.dumps(claims_)
    signing_input = b64(json.dumps(head).encode()) + "." + b64(body.encode())
    der = keys[signer or kid].sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    if high_s:
        s = N - s
    raw = bytearray(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    if flip:
        raw[-1] ^= 1
    # The signature itself malformed: r‖s is 64 bytes, each value in [1, n-1].
    raw = {None: bytes(raw), "der": der, "s padded": bytes(raw[:32]) + b"\0" + bytes(raw[32:]),
           "truncated": bytes(raw[:63]), "s = n": bytes(raw[:32]) + N.to_bytes(32, "big"),
           "r zero": bytes(32) + bytes(raw[32:])}[shape]
    return signing_input + "." + b64(raw)
off_curve = json.loads(json.dumps(jwks))
y = int.from_bytes(base64.urlsafe_b64decode(off_curve["keys"][0]["y"] + "=="), "big")
off_curve["keys"][0]["y"] = b64(((y + 1) % (2**256 - 2**224 + 2**192 + 2**96 - 1)).to_bytes(32, "big"))
cases = {
    "valid": token(claims()),
    "high s": token(claims(), high_s=True),
    "test issuer": token(claims(iss="test.idside.eu")),
    "exp written 1800000890.0": token(None, raw_claims=json.dumps(claims()).replace(f'"exp": {NOW + 890}', f'"exp": {NOW + 890}.0')),
    "tampered signature": token(claims(), flip=True),
    "signature in DER": token(claims(), shape="der"),
    "s padded to 33 bytes": token(claims(), shape="s padded"),
    "signature truncated": token(claims(), shape="truncated"),
    "s = n": token(claims(), shape="s = n"),
    "r zero": token(claims(), shape="r zero"),
    "ACT key": token(claims(), kid="act-g1"),
    "policy kid, ACT key's signature": token(claims(), signer="act-g1"),
    "unknown policy key": token(claims(), kid="content-policy-g9", signer="content-policy-g1"),
    "typ act+jwt": token(claims(), header={"alg": "ES256", "typ": "act+jwt", "kid": "content-policy-g1"}),
    "alg none": token(claims(), header={"alg": "none", "typ": "content-policy+jwt", "kid": "content-policy-g1"}),
    "crit": token(claims(), header={"alg": "ES256", "typ": "content-policy+jwt", "kid": "content-policy-g1", "crit": ["x"]}),
    "other issuer": token(claims(iss="evil.example")),
    "clock ahead (expired on arrival)": token(claims(exp=NOW)),
    "clock behind (issued in the future)": token(claims(iat=NOW + 301)),
    "expires before it was issued": token(claims(iat=NOW + 10, exp=NOW + 5)),
    "wrong digest": token(claims(digest="sha256:" + "0" * 64)),
    "another application": token(claims(app_id="app_other")),
    "schema 2": token(claims(policy={**policy, "schema": 2}, digest=content_policy_digest({**policy, "schema": 2}))),
}
json.dump({"now": NOW, "jwks": jwks, "off_curve": off_curve,
           "cases": [{"name": name, "token": tok, "jwks": "jwks"} for name, tok in cases.items()]
                    + [{"name": "key off the curve", "token": cases["valid"], "jwks": "off_curve"}]},
          open(sys.argv[1], "w"))
SIGN
)
py=$(cd packages/python && uv run python - "$cases_file" <<'READ'
import json, sys
from idside._content_policy import read_answer, verify_pending
from idside.exceptions import IDSideError
data = json.load(open(sys.argv[1]))
for case in data["cases"]:
    answer = {"enabled": True, "policy": case["token"], "refresh_after": 300,
              "challenge": {"token": "chl_parity", "inputs": ["marie@example.com"]}}
    try:
        state = verify_pending(read_answer(answer), data[case["jwks"]], data["now"])
        print(case["name"], "->", "applied", state.digest, state.declaration["challenge"]["response"])
    except IDSideError as error:
        # A clock out of step is told in the same words by both: compared too.
        said = f" {error}" if case["name"].startswith("clock") else ""
        print(case["name"], "->", type(error).__name__ + said)
READ
)
nd=$(cd packages/node && node --experimental-strip-types -e "
import {readFileSync} from 'node:fs';
import {readAnswer, verifyPending} from './src/contentPolicy.ts';
import {IDSideError} from './src/exceptions.ts';
const data = JSON.parse(readFileSync(process.argv[1], 'utf8'));
for (const c of data.cases) {
  const answer = {enabled: true, policy: c.token, refresh_after: 300, challenge: {token: 'chl_parity', inputs: ['marie@example.com']}};
  try {
    const state = verifyPending(readAnswer(answer), data[c.jwks], data.now);
    console.log(c.name, '->', 'applied', state.digest, state.declaration.challenge.response);
  } catch (error) {
    if (!(error instanceof IDSideError)) throw error;
    const said = c.name.startsWith('clock') ? ' ' + error.message : '';
    console.log(c.name, '->', error.constructor.name + said);
  }
}" "$cases_file")
if [ "$py" = "$nd" ]; then
  echo "$py" | sed -E 's/ (sha256:[0-9a-f]{12})[0-9a-f]+/ \1…/g; s/^/  ✅ /'
else
  echo "  ❌ DIVERGENCE"
  diff <(echo "$py") <(echo "$nd") | sed 's/^/     /'
  status=1
fi

exit "$status"
