---
title: What a turn costs
---

# What a turn costs

Companion to [cadre-ai-agent.md](cadre-ai-agent.md), which owns answer
*quality*. This page owns answer *cost*, and exists because until issue #79
there was nothing to write: `totalCost` read `0` on all 924 traces in the
project, because the model path is plain `httpx` (ADR 0002) and nothing was
recording token usage. Every figure below is read back from a real trace
through the Langfuse public API, not estimated.

The headline is not the one you would guess from the model roster.

## Where the money goes

One answered turn with the knowledge base live — trace
`3f80ce53fa8d6fafd6dd9c4cd8b27512`, **$0.0017 total**:

| step | model | tokens in | tokens out | cost | share |
|---|---|---:|---:|---:|---:|
| `brain` | `qwen.qwen3-32b` | 5576 | 167 | $0.00094 | 55% |
| **`output_safety`** | `qwen.qwen3-next-80b-a3b-instruct` | **4581** | **2** | **$0.00064** | **38%** |
| `topic_classifier` | `mistral.ministral-3-8b-instruct` | 463 | 3 | $0.00007 | 4% |
| `injection_check` | `mistral.ministral-3-8b-instruct` | 188 | 2 | $0.00003 | 2% |
| `validate_input` | `nvidia.nemotron-nano-12b-v2` | 119 | 3 | $0.00003 | 1% |
| `embedding` | `text-embedding-3-large` | 7 | — | $0.0000009 | 0.05% |

Two facts fall out of that table immediately.

**The output guard costs 38% of the turn to say one word.** It emitted two
tokens. Its entire expense is *input*: it re-reads the complete answer plus
every retrieved passage, because since issue #70 the guard judges groundedness
against the sources the brain actually used — the fix for the ten correct
answers that were wrongly retracted. That fix was right, and this is its
invoice.

**This turn is ~97% input tokens.** 10 934 in, 177 out. Output pricing is
where the roster's headline numbers differ most, and it is very nearly
irrelevant here.

## The retrieved passages are charged twice

The same turn's shape, against a turn on the same deployment with
`CADRE_KB_ENABLED=0` (trace `81dd32758f3413098e89032fc7cc4b3c`, **$0.00037**):

| | with KB | without KB | delta |
|---|---:|---:|---:|
| `brain` input tokens | 5576 | 1145 | +4431 |
| `output_safety` input tokens | 4581 | 419 | +4162 |
| turn cost | $0.0017 | $0.00037 | **+$0.0013** |

Roughly **4.3K tokens of passages, billed once to the brain and again to the
guard**, and on these two samples the retrieval context accounts for about
three quarters of what a turn costs.

Treat that ratio as indicative rather than measured: the two turns asked
different questions and produced different-length answers, so this is not a
controlled experiment. The *mechanism* — the same passages paid for twice — is
structural and not in doubt; the exact multiple is worth re-measuring on
matched questions before anyone acts on it.

## Which levers actually matter

Ranked by what the data supports, not by what looks tunable.

### 1. Passage volume — the only large lever

`RETRIEVE_TOP_K` is 6, over-fetched from `RETRIEVE_FETCH_K` 18 and deduped to
at most `RETRIEVE_MAX_PER_URL` 2 per page. Because every kept passage is paid
for twice, cutting the slate is the one change that moves both dominant lines
at once. Trimming chunk length has the same effect without changing which
pages are cited.

The catch is that issue #70 *raised* effective passage coverage on purpose:
the on-point article chunk was being crowded out by the homepage. Cutting
top-k blindly re-opens the failure this pipeline was tuned to fix, which is
exactly why this page ranks the lever and does not pull it.

### 2. Give the guard less than the brain gets

Nothing requires the two to read the same context. The brain needs the
passages to *write* from; the guard needs enough to check that the answer's
claims appear in the sources. Sending the guard a reduced slate — or only the
passages the answer plausibly draws on — would cut ~38% of turn cost roughly
in proportion, and it is independent of lever 1.

This is the change most likely to pay off, and the one that most needs a
groundedness benchmark run alongside it: the guard's fixture set is the ten
wrongly-retracted answers, and a guard that sees less is a guard that can start
retracting them again.

### 3. Prompt caching — unverified, potentially free

The guard and brain prompts have a large stable prefix (persona, topic scope,
instructions) and a variable tail (passages, answer). If the Mantle endpoint
supports prompt caching, the stable half stops being billed per turn.

**This is unverified.** Nobody has checked whether Bedrock's OpenAI-compatible
Mantle surface exposes cache controls at all. Check before designing around it,
the same way `stream_options.include_usage` was checked against the live
endpoint rather than assumed.

### 4. Things that look like levers and are not

- **Swapping the guard to a cheaper model.** The roster's *input* prices are
  nearly identical — `qwen3-next-80b-a3b-instruct` is \$0.14/1M in,
  `qwen3-32b` is \$0.15/1M. The 80B model is *cheaper* per input token than the
  32B one. Since the guard's cost is ~all input, this saves nothing and costs
  accuracy (48/48 vs 45/48 on the guard fixture set). The expense is token
  volume, not the model.
- **Lowering `BRAIN_MAX_TOKENS`.** Brain output is 167 tokens, $0.0001, 6% of
  the turn. It is also the only thing the visitor actually reads.
- **Tuning the small judges.** `validate_input`, `injection_check` and
  `topic_classifier` are 7% combined. The embedding is 0.05%.

## Dollars are not the only budget

Two other costs share this page and behave differently:

- **Latency.** The turn budget is CloudFront's hard 60s origin cap (KB-004),
  and the guard's 4.5K-token input is spent inside it. Cutting passage volume
  buys latency as well as money.
- **Quota.** Langfuse Cloud's free tier suspends ingestion with a 403 when the
  usage threshold is exceeded, and because tracing is fail-open that 403 is
  *swallowed* — traces silently stop existing while the product keeps answering
  (KB-021). Trace payload volume is therefore its own budget, unrelated to
  Bedrock spend, which is why the trace records a 500-character cap on raw
  model output and no chunk text at all.

## Measuring a change

Every one of the levers above is now falsifiable before and after, which was
the point of putting `cost_details` on each generation rather than configuring
prices in the Langfuse UI:

- per-generation cost, model id and token counts on every trace;
- `totalCost` per turn, aggregatable per model in the Langfuse UI;
- `backend/scripts/assert_trace.py` to fetch a turn back and read the figures
  directly.

Prices come from `MODEL_PRICES` in `backend/app/config.py`, sourced from the
AWS Price List API keyed on the exact model id, and a unit test fails the build
if a configured model has no price line — so a model swap cannot silently zero
these numbers.
