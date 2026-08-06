"""SSE wire format.

This is the contract with `web/` — the React trace panel and the Vitest suite
both code against exactly these four events. Renaming a field here is a
breaking change even though nothing imports this module across the boundary,
so the names are kept verbatim rather than prettified.

    event: rail   data: {rail_id, rail_name, passed, latency_ms, reason, degraded}
    event: token  data: {text}
    event: done   data: {refused, refusal_reason, latency_ms}
    event: error  data: {message}
"""

from __future__ import annotations

import json

# Rails in execution order. The frontend renders all six as pending up front
# and resolves them individually as events arrive, so this list and
# web/src/types.ts RAIL_SPECS must agree.
RAILS: list[tuple[str, str]] = [
    ("rail1", "input_validation"),
    ("rail2", "injection"),
    ("rail3", "topic"),
    ("rail4", "brain"),
    ("rail5", "output_guard"),
    ("rail6", "scrub"),
]


def rail(
    rail_id: str,
    rail_name: str,
    passed: bool,
    latency_ms: float,
    reason: str,
    degraded: bool = False,
) -> str:
    payload = {
        "rail_id": rail_id,
        "rail_name": rail_name,
        "passed": passed,
        "latency_ms": round(latency_ms, 1),
        "reason": reason,
        "degraded": degraded,
    }
    return f"event: rail\ndata: {json.dumps(payload)}\n\n"


def token(text: str) -> str:
    return f"event: token\ndata: {json.dumps({'text': text})}\n\n"


def done(refused: bool, refusal_reason: str | None, latency_ms: float) -> str:
    payload = {
        "refused": refused,
        "refusal_reason": refusal_reason,
        "latency_ms": round(latency_ms, 1),
    }
    return f"event: done\ndata: {json.dumps(payload)}\n\n"


def error(message: str) -> str:
    return f"event: error\ndata: {json.dumps({'message': message})}\n\n"
