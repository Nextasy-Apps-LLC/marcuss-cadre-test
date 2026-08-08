"""Benchmark candidate models for the three judge slots (issue #70).

Runs the labelled fixture sets under `evals/fixtures/` against real models on
the Bedrock Mantle endpoint, through the production prompt templates and the
production verdict parser (`app.graph.models._label`) — a raw string match
would flatter reasoning models whose verdict needs stripping first (KB-011),
and a bespoke prompt would benchmark something the pipeline never runs.

Per model and slot it reports: HTTP success rate over every call (KB-012 —
one call proves nothing about a model that 503s intermittently), accuracy
through the real parser, and p50/p95 wall latency including the transport's
bounded retries (KB-013) — because that is the latency the 60s turn budget
(KB-004) actually pays.

Usage (needs AWS_BEARER_TOKEN_BEDROCK in the environment):

    python -m evals.judge_bench --list-models
    python -m evals.judge_bench --slot all --models google.gemma-3-12b-it,qwen.qwen3-32b
    python -m evals.judge_bench --slot topic --models ... --runs 2

Output is a markdown table per slot, ready for a PR body.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

from app import config, llm
from app.graph import models

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PASS_FAIL = {"pass": "pass", "fail": "fail"}


def load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["cases"]


def slot_calls(slot: str) -> list[tuple[str, str, str, dict, str]]:
    """(case_id, system, user, allowed, expected) per fixture case."""
    calls = []
    if slot == "topic":
        for c in load("topic_cases.json"):
            state = {"message": c["message"], "history": c["history"]}
            calls.append(
                (c["id"], models._TOPIC_SYSTEM, models._conversation(state),
                 models._TOPIC_LABELS, c["expected"])
            )
    elif slot == "injection":
        for c in load("injection_cases.json"):
            calls.append(
                (c["id"], models._INJECTION_SYSTEM, c["message"], PASS_FAIL, c["expected"])
            )
    elif slot == "guard":
        for c in load("guard_cases.json"):
            calls.append(
                (c["id"], models._guard_system(c["context"]), c["answer"],
                 PASS_FAIL, c["expected"])
            )
    else:  # pragma: no cover - argparse restricts choices
        raise ValueError(slot)
    return calls


async def run_model(model_id: str, slot: str, runs: int) -> dict:
    calls = slot_calls(slot)
    latencies: list[float] = []
    http_errors = 0
    no_verdict = 0
    wrong: list[str] = []
    total = 0
    for _ in range(runs):
        for case_id, system, user, allowed, expected in calls:
            total += 1
            started = time.monotonic()
            try:
                raw = await llm.chat(
                    model_id,
                    system,
                    [{"role": "user", "content": user}],
                    max_tokens=config.JUDGE_MAX_TOKENS,
                    temperature=0.0,
                )
            except Exception as exc:  # noqa: BLE001 - an outage is a data point
                http_errors += 1
                wrong.append(f"{case_id}(error:{type(exc).__name__})")
                continue
            finally:
                latencies.append(time.monotonic() - started)
            label = models._label(raw, allowed)
            if label is None:
                no_verdict += 1
                wrong.append(f"{case_id}(no_verdict)")
            elif label != expected:
                wrong.append(f"{case_id}(got:{label})")
    correct = total - len(wrong)
    return {
        "model": model_id,
        "slot": slot,
        "total": total,
        "correct": correct,
        "http_errors": http_errors,
        "no_verdict": no_verdict,
        "accuracy": correct / total if total else 0.0,
        "p50_s": statistics.median(latencies) if latencies else None,
        "p95_s": (sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None),
        "wrong": wrong,
    }


async def list_models() -> list[str]:
    async with httpx.AsyncClient(
        base_url=config.BEDROCK_MANTLE_BASE_URL, timeout=30
    ) as client:
        response = await client.get(
            "/models", headers={"Authorization": f"Bearer {llm.api_key()}"}
        )
        response.raise_for_status()
        return sorted(m["id"] for m in response.json()["data"])


def table(results: list[dict]) -> str:
    lines = [
        "| model | acc | correct | http err | no verdict | p50 s | p95 s |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (-r["accuracy"], r["p50_s"] or 9e9)):
        lines.append(
            f"| {r['model']} | {r['accuracy']:.0%} | {r['correct']}/{r['total']} "
            f"| {r['http_errors']} | {r['no_verdict']} "
            f"| {r['p50_s']:.2f} | {r['p95_s']:.2f} |"
            if r["p50_s"] is not None
            else f"| {r['model']} | {r['accuracy']:.0%} | {r['correct']}/{r['total']} "
            f"| {r['http_errors']} | {r['no_verdict']} | - | - |"
        )
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", choices=["topic", "injection", "guard", "all"], default="all")
    parser.add_argument("--models", help="comma-separated model ids")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--json", help="also dump full results to this path")
    args = parser.parse_args()

    if args.list_models:
        for model_id in await list_models():
            print(model_id)
        return

    if not args.models:
        parser.error("--models is required unless --list-models")

    slots = ["topic", "injection", "guard"] if args.slot == "all" else [args.slot]
    model_ids = [m.strip() for m in args.models.split(",") if m.strip()]

    all_results: list[dict] = []
    for slot in slots:
        results = []
        for model_id in model_ids:
            result = await run_model(model_id, slot, args.runs)
            results.append(result)
            misses = ", ".join(result["wrong"][:6])
            print(
                f"[{slot}] {model_id}: {result['correct']}/{result['total']} "
                f"p50={result['p50_s'] and round(result['p50_s'], 2)}s"
                + (f"  misses: {misses}" if misses else "")
            )
        print(f"\n### {slot}\n\n{table(results)}\n")
        all_results.extend(results)

    if args.json:
        Path(args.json).write_text(json.dumps(all_results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
