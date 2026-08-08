# evals/ — offline evaluation against real endpoints

Build-time code, like `ingest/`: never imported by `app/`, never shipped in
the image. Runs on a laptop (or `workflow_dispatch`) with real credentials.

## Judge benchmark (`judge_bench.py`)

Benchmarks candidate models for the three judge slots — topic classifier,
injection check, output-safety guard — over the labelled fixture sets in
`fixtures/`, through the production prompts and the production verdict parser
(KB-011). Reports accuracy, HTTP success and p50/p95 latency per model
(KB-012); the defaults in `app/config.py` are picked from these numbers,
accuracy first, then latency against the 60s turn budget (KB-004).

```sh
export AWS_BEARER_TOKEN_BEDROCK=…   # from SSM /cadre/bedrock-api-key
python -m evals.judge_bench --list-models
python -m evals.judge_bench --slot all \
    --models google.gemma-3-12b-it,qwen.qwen3-32b,nvidia.nemotron-nano-12b-v2
```

## Fixtures (`fixtures/*.json`)

- `topic_cases.json` — labelled conversations (`in_scope` / `off_topic` /
  `needs_human`). The real cases carry their Langfuse trace ids, including
  the 2026-08-08 escalation-loop transcript (issue #70).
- `injection_cases.json` — labelled messages (`pass` / `fail`), including the
  meta-complaint that was wrongly refused.
- `guard_cases.json` — labelled (answer, context) pairs for the output guard,
  including the ten correct fact-dense answers that were wrongly retracted,
  each grounded in its real corpus passage.

The unit suite (`tests/test_answer_quality.py`) asserts the schema and the
presence of the regression cases; this harness spends the real model calls.
Every case's `source` names where it came from — keep that when adding cases.
