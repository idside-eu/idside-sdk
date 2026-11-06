# Commercial license

The SDK in this repository is **source-available, not open source**. It is
licensed under the ID Side Source-Available License, version 1.0 — see
[`LICENSE`](./LICENSE), also published at
<https://sdk.idside.eu/legal/sdk-licence>. This page says who needs a commercial
license and who does not. Where it and `LICENSE` differ, `LICENSE` governs.

## You do not need one

Reading the code. Evaluating it internally. Modifying it. Running it in an
isolated, non-production test environment. Research, and non-commercial
testing. Clause 2 of `LICENSE` grants a royalty-free licence to reproduce,
execute and modify the SDK solely for non-production internal evaluation,
research, and non-commercial testing, with a patent licence limited to the same
purpose. None of that costs anything, or requires talking to us first.
Redistributing the SDK, sublicensing it or hosting it as a managed service does
require our prior written consent.

That is deliberate rather than generous: a proof system you cannot read is a
proof system you have to take on faith, which is the opposite of what this
product sells. Every line that decides what leaves your process is here, and the
[ACT token specification](./docs/spec/act-token.md) is published so that a token
can be verified by someone who has never heard of us.

## You need one

Production. Anything that serves third parties, or that generates, issues,
validates or consumes proof objects outside an isolated, non-production test
environment. Building a competing product on this code, at any scale. Clause 3
of `LICENSE` places every such Commercial Purpose outside clause 2.

Beyond the limited evaluation patent licence of clause 2 of `LICENSE`, rights
under the patents behind the software are conveyed only by an executed
commercial licence. They are listed, and kept current, at the address the
software is marked with:

Patents: https://sdk.idside.eu/patents

- U.S. Patent No. 12,413,397 B2;
- European Patent No. 4,100,905 B1, with unitary effect, and any national
  patent rights deriving from that European patent;
- Chinese Patent Application No. CN115151937A, which is pending: no patent
  rights exist in an application before it issues.

Those terms are set out in a Master SDK License and Services Agreement, which
carries a data processing agreement, a service level agreement, an acceptable
use policy and an AI Act annex. Ask for it at the address below.

## Why it is arranged this way

A patent and a permissive public license do not survive each other. The terms
this repository used to carry granted every recipient a perpetual, worldwide,
**irrevocable** patent license — so publishing under them would have given away
the invention to anyone who ran `git clone`, and invited the obvious question of
why a patent was filed at all.

The answer is not to hide the code. It is to separate two things that are
usually conflated: **reading** the implementation, which should be free because
auditability is the product, and **running** the invention commercially, which is
what the patent covers and what is licensed. So the source stays open to read and
the invention stays licensed to use.

For a company evaluating this, the practical consequence is small and worth
stating plainly: you can satisfy yourself that the SDK does what it claims,
line by line, before any commercial conversation begins.

## Self-hosting, and the one mode it makes sense in

**Which deployment applies is settled in the agreement and not on this page**:
the order form names the model. What belongs here is the reason the answer is
not the same in both of the product's modes, because that reason is the whole
argument:

- **Asked** — a visitor answers the Preference Center and the legal basis is
  their consent. Here the cloud exists to be a **trusted third party**, and that
  only works if you cannot hold its signing keys or rewrite its ledger. An
  instance you operate yourself would prove nothing to a stranger, because you
  would be attesting to your own compliance with keys you control. SaaS is not a
  commercial preference in this mode; it is what makes the proof worth anything.
- **Imposed** — an organization sets a policy for its own members, on a basis
  that is not consent. Nobody is being asserted over as a stranger: what is owed
  is transparency to the members themselves, to a works council and to a
  regulator, all of whom can be shown the policy and the ledger directly. That
  audience changes the calculation, and it is why self-hosting is defensible
  here and not in the other mode.

## Contact

**contact@idside.eu**

Useful in a first message: your organization, your industry, an estimate of
monthly AI-call volume, and any constraint on where data may be processed.
