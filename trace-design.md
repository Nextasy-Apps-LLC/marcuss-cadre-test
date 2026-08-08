# trace-design.md — Langfuse instrumentation that answers questions

This is the design document for the next generation of `backend/app/tracing.py`:
what a turn's trace must carry so that the next answer-quality incident is
root-caused by *reading the trace*, not by inferring from span order or grepping
CloudWatch. plan.md owns the architecture, fine-tuning.md owns the quality
loop; this file owns the trace contract. It is a design, not an implementation
— issue #75 tracks the doc, and the phased plan at the end defines the
implementation issues. `adr/README.md` says ADRs are decisions, not designs,
which is why this lives here and not in `adr/`.

Everything in this document is grounded in two bodies of evidence, both from
2026-08-08:

- **The four production incidents of fine-tuning.md Pass 1** (issue #70), each
  root-caused through these traces. One diagnosis was immediate, three were
  needlessly hard. They are the acceptance test for every field proposed below:
  a field that would not have shortened one of those diagnoses — or an obvious
  next one — does not ship.
- **The traces as they actually exist.** 924 traces inspected through the
  Langfuse public API (`GET /api/public/traces`), not through the SDK's
  documentation. Where this document says "today the trace contains X", that is
  an API read, not a guess. Where an SDK capability is claimed, it was checked
  against an installed `langfuse==4.14.3` — the version the production traces
  report in their own metadata — and marked **verified**; anything not checked
  is marked as such with the check to run.

## 1. What a trace holds today

An answered turn produces 13 observations. Verified against trace
`871e3c9d97843f0a05ba1c1f7a7c8b63` (session `d0e5285c…`, the fine-tuning.md
Pass 1 evidence session):

| observation | type | input / output | size |
|---|---|---|---|
| `LangGraph` (root) | CHAIN | entire `ConversationState`, in and out | 2.5 KB / 23.5 KB |
| `validate_input`, `injection_check`, `topic_classifier`, `retrieve`, `brain`, `output_safety` | CHAIN | entire state blob, in and out, per node | 2.5–23.5 KB each way |
| `route` ×3, `_after_topic` | CHAIN | state blob in, branch name out | 2.5–23.5 KB in |
| `retrieval` (hand-written, `record_retrieval`) | SPAN | `{query}` / `{hits:[{url,score}]}`, `hit_count` | 60 B / 549 B |
| `turn` (hand-written, `finalize_trace`) | SPAN | null / null; metadata `refused_step`, `latency_ms` map, `total_latency_ms` | 8 B |

That is roughly **170 KB of observation payload per answered turn, ~95% of it
six-plus copies of the same state blob** — the state grows `context` (~20 KB of
retrieved passages) at `retrieve` and every subsequent node span carries it
twice more. What is *not* anywhere in those 170 KB: any model id, any token
count, any judge verdict in a form you can read without expanding a JSON blob
and scanning `steps[]`, any cost (`totalCost: 0` on all 924 traces), any tag,
any statement on the trace root of what the visitor asked and what they ended
up seeing.

Two things in the current output are not merely missing but **wrong**, and the
implementation issue should fix them in passing:

1. **The trace root's input/output is the retrieval span's payload.** On every
   answered turn, the trace-level input reads `{query: "<condensed query>"}` and
   the output reads `{hits: [...]}` — the public trace claims the turn's input
   was the condensed retrieval query. Mechanism: `record_retrieval` creates its
   span at trace root level (it has `trace_context` but no parent), Langfuse
   derives trace IO from root-level observation IO, and the retrieval span's
   write lands after the LangGraph root's in upsert order — the same
   last-write-wins behaviour `finalize_trace`'s double-flush comment documents
   for `public`/`session_id`. On refused turns (no retrieval span) the root IO
   is the raw state blob instead. Verified on traces `871e3c9d…` (answered) and
   `1b129801…` (refused). The fix is to stop relying on upsert order at all:
   the `turn` span already runs last and its handle has `set_trace_io()`
   (verified in 4.14.3) — set trace IO explicitly there (§4.9).
2. **`record_retrieval`'s stated purpose is defeated by its call site.** The
   docstring argues scores are "the only way to tell 'the corpus had nothing'
   from 'the floor is set too high'", but `nodes._retrieve` passes the list
   *after* the `RETRIEVE_MIN_SCORE` filter — and since #70, after per-URL
   dedupe and the top-k cut as well — so a floor-suppressed retrieval records
   `hits: []`, byte-identical to an empty corpus. PR #63's review flagged this
   (minor comment 3); #70's dedupe made it a three-way ambiguity. §4.4.

## 2. Principles — what earns a place on a trace

`record_retrieval`'s docstring already argues the discipline for one span: each
of query/URLs/scores is there because it answers a question ("was the rewrite
bad", "what might the visitor read", "floor or empty corpus"), and chunk text
is excluded because it is duplicated in the brain span and would make a public
trace expensive to load for no new fact. Generalised:

1. **A field must name the debugging question it answers.** The inventory in
   §3 has a question column; a proposed field that cannot fill that column in
   one sentence is dropped. "Might be useful someday" is how 170 KB state
   blobs happen.
2. **Never record a fact twice.** The state blob currently carries the answer
   seven times. One observation owns each fact; everything else references it
   by being on the same trace. This is not tidiness: Langfuse Cloud's free
   tier suspends ingestion with a 403 when the usage threshold is exceeded,
   and because tracing is fail-open that 403 is *swallowed* — traces silently
   stop existing while the product keeps answering (KB-021, already hit by a
   sibling project on this same tier). Payload volume is quota, and quota
   exhaustion here is invisible by design.
3. **These traces are public — write for a hostile reader.** The trace URL
   goes out on the wire to every visitor. plan.md accepts that user messages
   are exposed (a demo trade-off, listed under scope). What must **never**
   appear, under that same public URL: credentials or key material of any
   kind; full system-prompt text (record the prompt *file name* and a content
   hash instead — the prompts are versioned in `app/prompts/*.txt`, so a hash
   names the exact version without republishing the text, and a public trace
   never becomes the canonical leak of a prompt change that has not deployed
   yet); anything about a *different* visitor's turn (session isolation is
   KB-008's whole point). Judge raw outputs are borderline — §4.3 takes a
   position.
4. **Literals over nulls.** Langfuse drops metadata keys whose value is null,
   so an absent field is indistinguishable from instrumentation that failed to
   run — the exact shape of KB-009. `NOT_REFUSED = "none"` in `tracing.py` is
   this rule already applied once; every field below follows it (e.g. a
   generation that got no usage records `usage_source: "absent"`, not
   nothing).
5. **Reuse measured values, never re-measure.** Per-step latencies on the
   trace are the `elapsed_ms` values already on the SSE wire, handed down —
   the trace and the stepper cannot disagree. The same applies to token
   counts: they come from the provider's `usage` object, never from a local
   tokenizer that can drift from what is billed.
6. **Tracing stays fail-open, and fail-open stays visible.** Every recording
   function swallows exceptions and logs (module invariant); §6 adds the other
   half — a readback check that catches the span that silently no-ops, because
   the root-IO clobber in §1 shipped precisely because nothing read the trace
   back.

## 3. The field inventory

The turn root and six steps, with the question each field answers. Fields
marked ● exist today; everything else is proposed. Shapes are exact — the
implementor should not have to invent one.

### The trace root (`turn` span + trace-level attributes)

| field | where / shape | question it answers |
|---|---|---|
| session id ● | trace `sessionId` = `client_id` | "show me this visitor's whole conversation" |
| `refused_step` ● | metadata, literal (`"none"` when clean) | "which rail fired" |
| `latency_ms` map + `total_latency_ms` ● | metadata | "where did the time go" |
| trace input | `{message, history_turns: <int>}` via `set_trace_io` | "what did the visitor actually ask" — today the root claims the condensed query was the input (§1.1) |
| trace output | `{outcome, answer_chars: <int>, refusal_text?}` | "what did the visitor actually see" — outcome is nowhere on the trace today |
| tags | `outcome:answered\|refused\|escalated\|error`, `refused:<step>` when refused, `degraded` when any step passed degraded, `kb:hit\|no_hits\|skipped` | "list every refused turn", "find the turns where a rail was down" — today answerable only by opening 924 traces one at a time |
| `environment` | `Langfuse(environment=CADRE_ENV)` at `_configure` | "is this prod or someone's laptop" — dev turns pollute every aggregate today |
| `total_cost_usd` | derivable by Langfuse from per-generation `cost_details` (§4.7) | "what does a turn cost" |

### `validate_input` / `injection_check` (the same shape, two steps)

| field | shape | question it answers |
|---|---|---|
| generation: model id | `model=<effective id>` on a hand-built generation (§4.2) | "which model judged this" — env-overridable ids mean the code default is not an answer |
| generation: usage | `usage_details={input, output, total}` from Mantle's `usage` (§4.6) | "what does this rail cost per turn"; an input-token outlier means history/scope bloat |
| generation: output | `{raw: "<first 500 chars>", verdict, detail}` | "did the parser read the model right" — `_label`'s last-match-wins parse is subtle enough to have its own KB entry family (KB-011) |
| `degraded_reason` | metadata on the step's verdict, exception class name (e.g. `HTTPStatusError:503`), `"no_verdict"` for an unparseable answer | "why did this rail wave the turn through" — today `detail:"degraded"` conflates outage, bad key, and monologue-truncation, and the cause lives only in CloudWatch |

### `topic_classifier`

Everything above, plus the chain:

| field | shape | question it answers |
|---|---|---|
| one generation per *attempt* | errored attempts recorded at `level=ERROR` with `status_message=<exception class>`; the answering attempt is a normal generation | "which model actually answered" — `classify_topic` walks `MODEL_TOPIC` then `MODEL_TOPIC_FALLBACKS` on errors and today discards the loop variable; a trace naming the configured primary when `zai.glm-4.7-flash` answered is worse than no data |
| `fallback_index` | metadata on the answering generation (`0` = primary) | "how often is the primary actually down" — aggregate over a week and the fallback chain's ordering stops being a guess |
| verdict | output `{raw, verdict: in_scope\|off_topic\|needs_human, detail}` | incident 2: "why did the turn escalate before retrieval ran" — today the label is buried in the state blob's `steps[]` |

### `retrieve`

The one step that is already half-right — `record_retrieval`'s condensed query
and hit scores made incident 3 the *easy* diagnosis, and its shape is the model
for the rest. What it still needs:

| field | shape | question it answers |
|---|---|---|
| input ● (extended) | `{raw_query, condensed_query}` — both, always; equal strings on a first message | "did the condenser change the meaning" — today only the condensed form is recorded, so the *delta* (incident 3's actual evidence) must be reconstructed from the state blob |
| condenser generation | model id + usage + `{raw, kept_query}` output; `condense_used: false` literal on first messages | "did the fallback-to-raw-words fire, and why" — a `_plausible_query` rejection is invisible today |
| embedding observation | `as_type="embedding"`, `model=EMBEDDING_MODEL`, `usage_details` from the OpenAI response | "what does retrieval cost"; a runaway condensed query is visible **only** as an embedding token outlier — the condenser's char cap (`CONDENSE_MAX_CHARS`) bounds it, but bounded is not observed |
| output ● (split) | `{fetched: [{url,score}] (pre-floor, ≤RETRIEVE_FETCH_K), kept: [{url,score}] (the slate the brain saw)}` | "empty corpus vs floor vs dedupe" — the three-way ambiguity of §1.2. `fetched` at 18 entries of `{url,score}` is ~1.6 KB; chunk text stays excluded (unchanged rule) |
| metadata | `{floor, top_k, fetch_k, max_per_url, fetched_count, kept_count}` | "what were the knobs when this turn ran" — config is env-overridable, so the repo's defaults are not evidence |
| skip cause ● (on trace) | already on the wire as `detail`; surfaces via the `kb:skipped` tag + `degraded_reason` | incident 1: the `kb_timeout` detail was the part of the trace that *worked* — keep it, make it filterable |

### `brain`

| field | shape | question it answers |
|---|---|---|
| generation | `model`, `model_parameters={max_tokens, temperature}`, usage via `stream_options.include_usage` (§4.6), `completion_start_time` at first delta | "which brain, what did it cost, how long to first token" — TTFT is the number that explains a sluggish-feeling turn whose total latency looks fine |
| input | `{system_prompt_file: "system.txt", system_prompt_sha256, context_chars, history_turns, message_chars}` | "which persona version wrote this, with how much context" — without republishing the prompt text or a third copy of the passages (principle 3 and 2) |
| output | `{answer_chars, finish_reason}` | "was the answer truncated" — `BRAIN_MAX_TOKENS` truncation currently looks identical to a natural ending; the answer text itself is already on the trace root output, not duplicated here |

### `output_safety`

The undiagnosable step — incident 4's ~10 retractions of factually correct
answers could not be attributed beyond "the guard said fail":

| field | shape | question it answers |
|---|---|---|
| `scrub_rule` | `"external_url" \| "pii:<pattern name>" \| "none"` — the deterministic half's matched rule from `scrub_failure` | "did a regex or a model retract this" — two halves with different fix paths (a pattern to tune vs a prompt to tune), merged into one `fail` today |
| guard generation | model + usage + output `{raw: "<first 500 chars>", verdict, detail}` | "what did the guard actually say" — incident 4 becomes: open the trace, read the guard's own words. The #70 fix (guard now sees retrieved passages) was designed blind for want of exactly this field |
| `saw_context` | `true\|false` literal on the generation metadata | "did the guard judge against the passages or the baseline" — the precise mechanism of incident 4, now a boolean instead of an archaeology project |

On recording the guard's raw output on a **public** trace: `REFUSAL_TEXTS`
deliberately refuses to tell the visitor *why* ("an explanation of the check is
a map for getting around it"), and the trace link is handed to that same
visitor. This design accepts the exposure, for stated reasons: the guard
prompt is already public in this repo; the raw output is a verdict about text
the visitor has *already seen streamed*; and plan.md's public-trace trade
already exposes strictly more sensitive material (the user's own messages). The
alternative — a private field on a public trace — does not exist in Langfuse's
model. If that calculus changes, the lever is plan.md's "with more time" row
(per-conversation opt-in / trace redaction), not quiet field removal.

### The incident test

The four incidents, re-run against this inventory — the criterion every field
had to pass:

| incident (fine-tuning.md Pass 1) | field that makes the diagnosis immediate |
|---|---|
| 1 — cold-start `kb_timeout` → ungrounded sycophantic answer | `kb:skipped` tag to *find* it (was: worked, but unfindable); `degraded_reason` + brain input `context_chars: 0` to confirm the brain ran bare |
| 2 — escalated before retrieval ran | `topic_classifier` generation output `verdict: needs_human` with its `raw`, plus `outcome:escalated` tag — the route is stated, not inferred from which spans exist |
| 3 — condenser rewrote intent, article fell out of top-k | `{raw_query, condensed_query}` side by side + `fetched` vs `kept` showing where the article ranked pre-floor — was the one diagnosis the trace already made easy, completed |
| 4 — ~10 correct answers retracted, reason unknown | guard generation `raw` + `scrub_rule: none` + `saw_context: false` — the root cause (guard never saw the passages) is literally a field |

## 4. The gaps, argued

### 4.1 Judge verdicts in readable form

Every judge already returns `Verdict(verdict, detail)` (`app/graph/models.py`)
and every node already funnels it through `nodes._record` — the facts exist at
exactly one choke point each. Today they reach the trace only inside the state
blob's `steps[]`, where reading them means expanding a 23 KB JSON blob per
node. The fix is not new data, it is *placement*: the verdict rides the step's
hand-built generation output (§4.2), and the turn root grows nothing — the
existing `refused_step` plus the new tags cover the "at a glance" need.

### 4.2 Effective model id — generations, hand-built

The single most consequential change, and the coordinator-level requirement:
**every model-backed call becomes a hand-built Langfuse `generation` (the
embedding an `as_type="embedding"` observation), created in the transport where
the call happens.** ADR 0002 removed LangChain from the model path, so nothing
will ever auto-create these; issue #53 accepted losing them, and
`backend/CLAUDE.md` hardened that into "don't instrument `app/llm.py` to work
around it". This document supersedes that sentence, deliberately: the rule
guarded against *bespoke per-call-site logging duplicating the callback
surface*, but model id and token usage exist **only** inside the HTTP response
that `llm.chat` / `llm.chat_stream` / `embeddings.embed_query` currently parse
and discard — verified in code (`llm.py` reads only `choices[0].message`,
`embeddings.py` only `data[0].embedding`) and against the live endpoint
(§4.6). No other layer can ever know these numbers. The CLAUDE.md sentence
changes in the implementation PR, citing this section.

What a generation buys over a span (all verified in 4.14.3's
`start_as_current_observation` / `start_observation` signatures): the `model`,
`usage_details`, `cost_details`, `model_parameters` and
`completion_start_time` parameters — which is to say Langfuse's entire
model/token/cost UI, per-model aggregation, latency-per-token, and trace-level
cost rollup. Spans get none of that. What it costs in code: one fail-open
wrapper in `tracing.py` (~40 lines), used as

```python
gen = tracing.start_generation(step, model_id, params)   # before the httpx call
...
gen.finish(usage=data.get("usage"), raw=text)             # after; .end() inside
```

with `start_generation` returning a no-op object when tracing is down — the
transport never grows a Langfuse import beyond the one call, and the seam
stays monkeypatchable exactly like `_client()`.

**Attribution without threading ids.** `record_retrieval` takes `trace_id`
explicitly because the retrieve node holds it (on `emit`, KB-008). The
transport does not and must not — threading `trace_id` through every
`models.py` seam signature would break the one-line-monkeypatch property the
whole test suite leans on. The v4 SDK is OTel underneath: an observation
opened with `start_as_current_observation` is ambient for everything inside
it, and Python contextvars propagate into `asyncio.create_task` — so `_stream`
in `main.py` opens the turn as the current observation for the duration of the
graph task, and every generation created inside the transport parents itself
with **zero** ids passed. This contradicts `tracing.py`'s "this module never
looks at ambient context" line; supersede it with its own reasoning preserved
— the line exists to prevent cross-visitor leakage, and task-local contextvars
are precisely the isolation it wanted. **Verify, don't trust** (§6): one unit
test that runs two interleaved fake turns and asserts no observation crosses
traces, plus the readback check. If verification falsifies the contextvar
propagation, the fallback is explicit: `llm.chat(..., trace_ctx=...)` threaded
from the nodes — uglier, equally correct, and the seams grow one keyword-only
argument with a `None` default so existing monkeypatches keep working.

The topic chain falls out for free: `classify_topic`'s loop makes one
transport call per attempt, so each attempt is its own generation — errored
ones at `level=ERROR` with the exception class as `status_message`, and the
answering one carrying `fallback_index`. The effective model is no longer
inferred; it is the generation that has output.

### 4.3 Guard and refusal reason

Covered in the §3 `output_safety` table and its public-trace note. The
implementation point: `scrub_failure` returns the rule name already —
`guard_output` throws it away by folding it into the verdict detail. Return it
alongside (or record it before the early return), and record the guard
model's raw text from the same `_judge` path every other step uses. Files:
`app/graph/models.py` (`guard_output`), `app/graph/nodes.py`
(`output_safety`).

### 4.4 Pre-floor vs post-floor retrieval

`nodes._retrieve` computes the unfiltered list and immediately filters it in a
comprehension; the change is to keep both and pass both:
`tracing.record_retrieval(trace_id, raw_query, condensed_query, fetched,
kept)`. Cost: ≤18 `{url, score}` dicts, ~1.6 KB — three orders of magnitude
under the state-blob noise this design removes. This closes PR #63 review
comment 3 and the dedupe blind spot #70 added on top of it.

### 4.5 Degraded-path attribution

Six fail-open `except` branches (`nodes.py`) plus three inside `models.py`
(`no_verdict` paths, condenser fallback) currently all collapse to
`detail:"degraded"` on the wire and a CloudWatch line nobody is reading during
an incident. Each branch names its cause as a literal (`degraded_reason`:
exception class, `no_verdict`, `implausible_rewrite`) on the step's
observation metadata. The wire contract does **not** change — `degraded`
remains the only detail value the client sees; the trace carries the diagnosis
(new SSE fields would be a KB-005 coordinated change for no visitor benefit).

### 4.6 Tokens in / out, per call — verified against the real endpoints

All three sources were probed live on 2026-08-08 with the production
credentials; none of this is assumed:

- **Mantle, non-stream** (`POST /chat/completions`): returns
  `usage: {prompt_tokens, completion_tokens, total_tokens}`. `llm.chat`
  parses `choices[0].message` and discards it. Every judge and the condenser
  get usage by reading a field that is already in the response in hand.
- **Mantle, stream**: a final `usage` chunk arrives **only** when the request
  carries `stream_options: {"include_usage": true}` — verified present with
  the flag (against `qwen.qwen3-32b`, the brain itself) and absent without.
  `chat_stream` adds the flag in `_payload` and captures the chunk it
  currently skips (its `if not choices: continue` line is exactly where the
  usage chunk passes through today). One caveat the implementor must honour:
  the flag's behaviour was verified on the current brain model; re-verify when
  any `CADRE_MODEL_*` default changes, in the same breath as
  `scripts/assert_models.py` — a model that ignores the flag degrades to
  `usage_source:"absent"`, never to an exception.
- **OpenAI embeddings**: returns `usage: {prompt_tokens, total_tokens}`;
  `embed_query` discards it. The condensed-query string from incident 3 costs
  exactly 7 tokens — small, but it is the *only* observable that catches a
  condenser writing essays before `CONDENSE_MAX_CHARS` bounds the damage.

Per answered turn that is 5–7 chat calls (validate, injection, topic ×1–3,
condense ×0–1, brain, guard) plus one embedding — every one of them a real
cost line that is currently invisible.

### 4.7 Cost — computed in-repo, not configured in a UI

Langfuse can price generations two ways: match `model` against model
definitions configured in the project UI, or accept explicit `cost_details`
per generation. **This project computes cost locally and sends
`cost_details`.** The argument: every model id is env-overridable
(`CADRE_MODEL_*`), so a UI-side pricing table matches against ids that change
without a deploy and drifts silently — the exact failure class
`scripts/assert_models.py` exists to prevent, invisible in any diff. An
in-repo table (`MODEL_PRICES: dict[str, tuple[float, float]]` — USD per 1M
input/output tokens — in `config.py` next to the ids it prices, populated
from the AWS Bedrock and OpenAI pricing pages at implementation time) is
reviewed like everything else here, and one unit test asserts every configured
`MODEL_*`/`EMBEDDING_MODEL` id has a price — a model swap without a price line
fails CI instead of zeroing a dashboard. Unknown id at runtime: record usage,
omit cost, log the warning (principle 4 — `cost_source: "unpriced"`).

What the numbers unlock beyond single-trace debugging, and why this lands in
Phase 1 rather than a nice-to-have tier: fine-tuning.md's judge benchmark
trades accuracy against latency and cannot see the third axis — the #70 guard
swap moved that slot from qwen3-32b to an 80B model with no way to say what it
did to per-turn cost. With per-generation `cost_details`, "which step
dominates turn cost", "did the swap pay for itself", and "why does this turn
have 10× the prompt tokens" (history or context bloat, the outlier that
predicts the next incident) are Langfuse UI queries, not engineering tasks.

### 4.8 Findability — tags and sessions

Session grouping exists (`client_id` via `propagate_attributes(session_id=…)`)
and works. Tags do not exist at all, so the trace list is 924 undifferentiated
rows of `turn` — during Pass 1 the evidence traces were found by *timestamp*.
The tag set in §3 (outcome, refused step, degraded, kb state) is deliberately
mechanical: each tag is a filter someone ran manually this week.
`propagate_attributes(tags=[...])` is verified in 4.14.3; tags ride the same
`finalize_trace` call that already sets the session. `environment=CADRE_ENV`
at `_configure` separates prod from laptop turns in every aggregate. `userId`
stays unused — there is no user identity in this product beyond `client_id`,
and inventing one would violate principle 3.

### 4.9 Trace root IO

`set_trace_io(input=…, output=…)` on the `turn` span handle (verified in
4.14.3), called in `finalize_trace` with the §3 root shapes. This replaces the
flush-order-luck mechanism that currently lets the retrieval span own the
trace root (§1.1), and it is the reason the fix is deterministic rather than
another ordering hack: explicit trace IO wins over derived-from-root-span IO.
Keep the existing double-flush regardless — it still protects
`public`/`session_id` — and re-verify both behaviours by readback after the
change, since both were discovered empirically in the first place.

## 5. Langfuse mechanics — how, precisely

**Spans vs generations vs events.** Generations for every model call and the
embedding (§4.2 — they alone unlock the model/token/cost UI). Spans for
structure: the ambient `turn`, `retrieval`. Events (`client.create_event`,
verified) for zero-duration facts — `degraded_reason` fires as an event on the
step when the fail-open path runs, so it is visible in the timeline without
inventing a duration. Ratio check: today's 13 observations become ~10–16
meaningful ones (6 step spans replacing 10 CHAIN blobs once §5-noise lands,
5–8 generations, retrieval, turn) — more observations, two orders of magnitude
less payload.

**Metadata vs input/output vs tags.** Input/output: what the component
consumed and produced, in the smallest form that answers the question —
that is what the UI renders as the observation's body. Metadata: the knobs and
attribution (config values, `fallback_index`, `degraded_reason`,
`saw_context`) — queryable, not rendered as payload. Tags: trace-level filters
only, never per-observation facts. The state blob belongs to none of these.

**Scores are reserved for quality judgments, and that is a decision.**
Mechanical facts (refused step, degraded, outcome) are tags/metadata —
filterable without polluting the score axis. Scores (`create_score`, verified:
trace- and observation-scoped, `NUMERIC`/`CATEGORICAL`/`BOOLEAN`, with
`comment`) are the landing zone for **judgments about answer quality**: Phase
4's eval harness writes its groundedness/correctness/persona rubric grades as
scores on the experiment runs' traces (plan.md already commits to Langfuse
experiments for run-vs-run comparison — scores are the native currency of that
UI), and the Phase 5 thumbs, when they stop being no-ops, write
`user_feedback` scores on the production turn's trace (plan.md's own "with
more time" row). Keeping the score namespace clean now is what makes
"average groundedness, this week vs last" mean something later.

**Flush before freeze — unchanged, and now it covers more.** Lambda freezes
the instance when the response ends; `finalize_trace` flushes before the
terminal SSE frame on every path. New observations created during the turn
ride the same batch; nothing about the generations changes the flush contract.
The one addition: generations created inside a *streaming* brain call must be
ended before `finalize_trace` runs — the wrapper's `finish()` in
`chat_stream`'s `finally` handles the mid-stream-failure path, so a turn that
dies mid-answer still ships a complete (ERROR-level) generation rather than an
unended one.

**Trace ids.** Everything continues to use the id minted by
`Langfuse.create_trace_id()` at `start_trace` — KB-019: the v4 SDK silently
discards foreign ids and the trace lands under a different one than the URL
on the wire.

## 6. Noise reduction — the per-node state blobs

The LangChain `CallbackHandler` produces the 10 CHAIN observations of §1:
per-node spans whose input/output is the entire state, plus `route` /
`_after_topic` plumbing spans. They cost ~160 KB of the ~170 KB per turn
(quota — principle 2), they bury the two hand-written spans that carry actual
signal, and their trace-attribute writes are the reason the double-flush hack
exists. Their *sole* unique contribution once §3 lands: node start/end
timestamps — which `nodes._record` already knows better (`elapsed_ms` is the
wire truth).

**Recommendation: replace the handler with explicit per-step spans, second
phase.** `nodes._record` is the single choke point every step transition
already passes through; opening a span per step there (inside the ambient turn
context, so parenting is free) reproduces the timeline with input/output
defined by §3 instead of by state serialization. Removing the handler then
deletes: the state-blob payload, the `route` noise, the upsert race (§4.9's
fix becomes belt-and-braces), **and the `langchain` dependency** — which
`requirements.txt` documents as existing *only* to satisfy
`langfuse.langchain`'s import guard. Cold-start weight, gone; `start_trace`
stops returning a handler; `main._run_graph` stops passing `callbacks`.

Not first, though — Phase 1 must not gut the only tree while the new
observations are proving themselves (§8 sequencing, and §7 is the proof).
Interim, if quota pressure demands it: the 4.14.3 client exposes `mask` (a
callable over IO payloads), `mask_otel_spans`, and `should_export_span` —
truncating state-blob values or dropping `route`/`_after_topic` spans at
export. **Unverified** whether `mask` reaches the LangChain handler's spans
with `mask_otel_spans` set — if Phase 1 wants it, verify by readback (§7)
before relying on it; if it does not reach them, skip the interim and let
Phase 2 remove the source.

## 7. Verifying instrumentation is real

The failure mode is silent success: a span that no-ops, fields that never
land, a trace that looks healthy because nobody looked. It has already
happened twice here — the root-IO clobber (§1.1) shipped unseen, and KB-021's
quota-403 is *designed* to be swallowed. Logging is not the check; **the check
is reading the trace back through the same public API a debugger would use.**

Prescription — `backend/scripts/assert_trace.py`, the same shape and CI
station as `assert_models.py`:

1. Run one answered turn and one refused turn against a target (`BASE_URL`;
   local = the real image in docker with real credentials, exactly like the
   e2e suite), capturing each `trace` SSE event's id.
2. Poll `GET /api/public/traces/{id}` (Basic auth from the env) until the
   observation count stabilises, **bounded at 90 s** — Langfuse Cloud
   ingestion is async and the lag is real and variable (measured 8–14 s,
   documented to 90 s: KB-020). A fixed short sleep is a flaky test by
   construction.
3. Assert the contract, not the vibes: root input contains the sent message
   and root output the outcome; every expected generation exists with
   `model != null` and `usage.total > 0` (or an explicit
   `usage_source:"absent"`); `retrieval` carries both `fetched` and `kept`;
   the tag set matches the outcome; the refused turn carries `refused:<step>`.
4. A 404 after the deadline or a missing field is a **hard failure** naming
   the field — and per KB-021, first check stderr for the quota-403 before
   debugging the code.

This runs in the e2e suite (`pytest -m e2e` — turns cost real money, same
policy as today) and once in the implementation PR's evidence. Two cheaper
guards run in the unit suite: the interleaved-turns contextvar isolation test
(§4.2), and the `MODEL_PRICES`-covers-every-configured-id assertion (§4.7).

## 8. Phased adoption

**Phase 1 — everything the last four incidents needed, plus the cost axis.**
One compound issue. In order of value per line of code: the generation wrapper
in `tracing.py` + ambient turn context in `main._stream` (§4.2); `usage`
capture in `llm.chat`, `stream_options` + usage-chunk capture in
`llm.chat_stream`, `usage` capture in `embeddings.embed_query` (§4.6);
verdict/raw/`degraded_reason` on the step generations (§4.1, 4.3, 4.5) —
including `scrub_rule` and `saw_context` on the guard; `record_retrieval`
extended to raw+condensed query and fetched+kept (§4.4); `MODEL_PRICES` +
`cost_details` (§4.7); root IO fix, tags, `environment` (§4.8, 4.9);
`assert_trace.py` (§7). Model ids and token counts are Phase 1 by requirement,
not preference — and nothing in this list touches the wire contract, so no
coordinated web PR.

**Phase 2 — structure.** Replace the `CallbackHandler` with `_record`-driven
step spans; drop the `langchain` dependency; retire or simplify the
double-flush after readback re-verification (§5, §6). Separate issue because
it changes the trace's skeleton and should land against a Phase 1 baseline
that `assert_trace.py` already guards.

**Phase 3 — the loop.** Score wiring for the Phase 4 eval harness and the
Phase 5 feedback UI (§5-scores). Owned by those phases' issues; this document
just reserves the namespace.

**Deliberately never:** chunk text on the retrieval span (the founding rule);
full system-prompt text on any observation (file + hash only); raw
credentials or key material anywhere; per-token events (a 700-token answer as
700 observations is quota suicide for zero questions answered); duplicating
the user message onto every judge generation's input (it is on the trace root
— principle 2); re-measuring anything the wire or the provider already
measured (principle 5).

## 9. Corrections to standing text

The implementation PR for Phase 1 must update, citing this document: the
"don't instrument `app/llm.py`" sentence and the trace-minimum list in
`backend/CLAUDE.md` (§4.2, §3); the "never looks at ambient context" line in
`app/tracing.py`'s docstring (§4.2); `record_retrieval`'s docstring, whose
empty-vs-floor claim is currently false at its call site (§1.2); and plan.md's
"Individual Bedrock calls are *not* captured as generations … that trade is
accepted" sentence, which this design supersedes.
