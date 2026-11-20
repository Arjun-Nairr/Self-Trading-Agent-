# Free tracing options for the trading agent

Research date: 2026-11-02. Primary documentation only. No services, SDKs or
credentials installed/tested; no purchases, Docker actions or runtime changes.

## Recommendation (engineering judgment)

Keep hosted Langfuse as the first candidate, not a guaranteed best product for
every workload. Its documented Hermes integration is particularly relevant to
our internal-tool visibility requirement. Verify the exact pinned gateway before
implementation. LangSmith is a strong alternative for many child operations per
cycle; Opik is attractive for a longer free review window. Do not self-host a
second platform on the trading VM simply to avoid a hosted quota.

## Verified free tiers

| Product | Published free allowance | History | Notes and primary sources |
|---|---|---|---|
| Langfuse Hobby | 50,000 units/month; two users; no card required | 30-day data access | [Pricing](https://langfuse.com/pricing), retrieved lines 47–67. |
| LangSmith Developer | 5,000 base traces/month; one seat | 14-day base retention | [Pricing](https://www.langchain.com/pricing), lines 10–22; [billing](https://docs.langchain.com/langsmith/billing), lines 67–73, 175–178. Personal accounts are capped until a card is added; adding one enables billed excess. |
| Opik Free Cloud | 25,000 spans/month; up to ten members | 60-day retention | [Comet pricing, Opik section](https://www.comet.com/site/pricing/). Do not confuse its separate MLOps tier with Opik. [Product page](https://www.comet.com/site/products/opik/) says no card is needed to sign up. |
| Arize AX Free | 25,000 spans/month and 1 GB ingestion/month; unlimited users; no card | 15-day retention | [Current pricing](https://arize.com/pricing/). Both volume limits matter for detailed payloads. |
| Phoenix OSS | Free self-hosted software; storage/compute managed by operator | Operator-managed | [Phoenix](https://arize.com/phoenix/), lines 41–58; [self-hosting](https://arize.com/docs/phoenix/self-hosting). Not equivalent to a hosted free allowance. |

Do not interpret a free-tier allowance as including model-provider calls or
paid evaluation runs. Do not add a payment method or activate paid extras as
part of implementation. Exact over-quota behaviour was verified for LangSmith;
Langfuse/Opik/AX enforcement details still require account-side verification.

## Counting and historical-access gotchas

- Langfuse units are traces plus observations plus scores. A detailed cycle can
  consume many units. [Billable units](https://langfuse.com/docs/administration/billable-units).
- A LangSmith trace groups the operation's child runs, not one allowance item
  per individual tool span. Manual tracing works outside LangChain.
  [Observability concepts](https://docs.langchain.com/langsmith/observability-concepts),
  sections on traces and manual instrumentation.
- Therefore 50,000 units is not necessarily more capacity than 5,000 traces.
  Illustration only: 1,000 cycles with 60 observations each and no scores would
  use 61,000 Langfuse units, versus 1,000 grouped LangSmith traces. This is an
  arithmetic example, not measured project traffic or a claim of unlimited
  child events; trace-size/ingestion limits still apply.
- Langfuse's 30 days is an access window, not an automatic deletion promise.
  [Access versus retention](https://langfuse.com/faq/all/data-access-vs-data-retention),
  lines 13–43. Preserve local evidence for future portfolio demonstrations.
- Current Phoenix landing links its cloud sign-in to AX. Older indexed claims
  of two free Phoenix Cloud instances do not establish current signup limits.
  [Phoenix landing](https://arize.com/phoenix/), lines 39–54. Do not recommend
  unlimited hosted Phoenix based on stale search snippets.

## Instrumentation and compatibility

- OpenTelemetry is vendor-neutral collection/export tooling, not a storage/UI
  backend. It complements these products rather than replacing them.
  [Official definition](https://opentelemetry.io/docs/what-is-opentelemetry/).
- Langfuse documents SDK/OpenTelemetry support on its pricing feature table.
  Opik accepts custom OTel spans over HTTP:
  [Opik integration](https://www.comet.com/docs/opik/integrations/opentelemetry),
  lines 27–58. LangSmith supports manual framework-independent instrumentation
  through its SDK ([concepts](https://docs.langchain.com/langsmith/observability-concepts)).
- Langfuse's [Hermes guide](https://langfuse.com/integrations/no-code/hermes)
  documents hooks for model requests and individual tool calls. This is evidence
  that an upstream integration exists in the documented environment, NOT that
  this project's pinned image includes it. Its linked old plugin README path
  returned 404 during this research; do not blindly copy installation commands
  or upgrade the gateway. Verify the runtime version, hook availability,
  redaction, parent-context propagation and bounded shutdown behaviour first.
- Project gateway pin: [docker-compose.yml](../../docker-compose.yml#L36).
  Cross-process seam: [live/agent.py](../../live/agent.py#L85).
  Cycle seam: [live/run_live.py](../../live/run_live.py#L78).
  These establish the need for internal hooks, not runtime compatibility with
  any newly selected SDK. No dependency versions have been selected or tested.

## Resource implications

The Langfuse Docker VM guide recommends at least four cores and 16 GiB, not a
measured hard minimum for every workload. This is already above the planned
two-core/12-GB trading VM; don't add self-hosted Langfuse to it by default.
[Docker VM guide](https://langfuse.com/self-hosting/deployment/docker-compose),
lines 70–75. Phoenix local hosting is a separate possible direction, not the
agreed hosted/no-extra-server plan. SDK peak RAM/CPU overhead is unmeasured.

## Project-specific decision and next step

1. Prefer hosted Langfuse if the pinned Hermes supports its tracing path without
   a broad runtime change. Existing spec remains a proposed design, not proof.
2. Measure units from one representative read-only cycle. Include reconciliation
   cycles, model turns, all tool calls and separately executed weekly screens
   in the monthly estimate. Count actual operations; do not suppress required
   evidence merely to claim the quota fits.
3. If full detail does not fit its free allowance, assess LangSmith's root-trace
   quota. If the priority becomes longer review access, assess Opik's 60 days.
4. Keep one backend. Retain Neon transaction records and local recovery/evidence;
   tracing remains best-effort and never blocks trading. No new collector stack.
5. All stated prices and limits are a dated snapshot. Confirm at signup before
   enabling export; no hosted account was accessed during this research.

No usage examples are presented as validated: the current task is product
selection, not SDK integration or an acceptance run.
