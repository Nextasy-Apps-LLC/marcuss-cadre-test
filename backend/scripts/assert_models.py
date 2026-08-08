"""Fail the deploy, not the first visitor, when a model id is wrong.

    python -m scripts.assert_models          # from backend/

Every model step in this service fails open. That is the right behaviour for
an outage and a terrible way to find out about a typo: a wrong model id does
not crash anything, it ships a chat whose guards all report `degraded` and
whose answers come from nowhere. The failure looks like a working product
(KB-009), which is exactly why it has to be caught before an image is pushed
rather than after.

So this asserts two separate things about every id in `app.config`, because
either one alone is misleading:

* **The model is listed** — `GET {MANTLE}/v1/models`. Catches typos and
  renames.
* **The account may actually invoke it** — a real one-token completion per id.
  Listing is not entitlement: several Claude ids appear in `/v1/models` on
  this account and return access-denied on invoke. That gap is the whole
  reason this script probes rather than trusting the catalogue.

The topic classifier is the one step allowed a hole: it has a fallback chain,
so the chain is satisfied by any one member. Everything else is required.

Exits 0 when the account can serve this build, non-zero naming what it cannot.
Needs `AWS_BEARER_TOKEN_BEDROCK` in the environment; it never prints it.
"""

from __future__ import annotations

import sys

from app import config


def topic_chain() -> list[str]:
    """The classifier and its fallbacks — the one step allowed a hole."""
    return [config.MODEL_TOPIC, *config.MODEL_TOPIC_FALLBACKS]


def hard_required() -> list[str]:
    """Steps with no fallback. Any one of these missing breaks a turn."""
    return [
        config.MODEL_VALIDATE,
        config.MODEL_INJECTION,
        config.MODEL_BRAIN,
        config.MODEL_GUARD,
    ]


def required_models() -> list[str]:
    """Every id this build may call, in the order a turn uses them.

    `MODEL_CONDENSE` is probed and printed here but is deliberately absent
    from `hard_required()`: when it is unavailable, `retrieve` embeds the
    visitor's message verbatim instead of a rewritten one. That is a worse
    query, not a broken turn — so it earns a visible `[MISS]` line, not a
    blocked deploy.
    """
    ordered = [
        config.MODEL_VALIDATE,
        config.MODEL_INJECTION,
        *topic_chain(),
        config.MODEL_CONDENSE,
        config.MODEL_BRAIN,
        config.MODEL_GUARD,
    ]
    return list(dict.fromkeys(ordered))


def missing_models(available: set[str]) -> list[str]:
    """Configured ids the account cannot serve, in declaration order.

    The two requirement kinds are evaluated separately rather than filtered
    out of one list, because the roster reuses ids across steps: Haiku is the
    injection judge, the output guard *and* the last topic fallback. Treating
    "is in the chain" as a blanket exemption would let a missing Haiku pass
    unreported purely because Nemotron was still up — the chain would survive
    and two steps with no fallback at all would silently be broken.
    """
    missing = [model_id for model_id in hard_required() if model_id not in available]

    chain = topic_chain()
    if not any(model_id in available for model_id in chain):
        # Nothing can classify. Report every member: an operator has to
        # restore one of them, and which one is their choice.
        missing.extend(model_id for model_id in chain if model_id not in missing)

    order = {model_id: i for i, model_id in enumerate(required_models())}
    return sorted(dict.fromkeys(missing), key=lambda m: order.get(m, len(order)))


def available_models(base_url: str | None = None) -> set[str]:
    """The configured ids this account can actually invoke, right now.

    Scoped to the configured ids rather than the whole catalogue: the probe is
    what makes the answer trustworthy, and spending a completion on each of 55
    listed models to answer a question about six would be slow and wasteful.
    """
    import httpx  # imported here so the pure helpers stay importable offline

    from app.llm import api_key

    base_url = (base_url or config.BEDROCK_MANTLE_BASE_URL).rstrip("/")
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}

    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        listed: set[str] = set()
        try:
            response = client.get("/models", headers=headers)
            response.raise_for_status()
            listed = {entry["id"] for entry in response.json().get("data", [])}
        except Exception as exc:  # noqa: BLE001 - reported as nothing available
            print(f"  ! GET {base_url}/models failed: {type(exc).__name__}")
            return set()

        available: set[str] = set()
        for model_id in sorted(set(required_models())):
            if model_id not in listed:
                print(f"  ! {model_id}: not in /models")
                continue
            # Listing is not entitlement. One cheap completion is the only
            # honest test of "can this build call this model".
            try:
                probe = client.post(
                    "/chat/completions",
                    headers=headers,
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "temperature": 0,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {model_id}: probe failed ({type(exc).__name__})")
                continue
            if probe.status_code == 200:
                available.add(model_id)
            else:
                # The body names the reason (access denied, not entitled, …).
                # It never contains the key.
                print(f"  ! {model_id}: listed but not invokable (HTTP {probe.status_code})")
    return available


def main(available: set[str] | None = None) -> int:
    base_url = config.BEDROCK_MANTLE_BASE_URL
    print(f"Checking Bedrock model access at {base_url}")

    if available is None:
        available = available_models(base_url)

    for model_id in required_models():
        mark = "ok  " if model_id in available else "MISS"
        print(f"  [{mark}] {model_id}")

    missing = missing_models(available)
    if missing:
        print(f"\nFAILED: {len(missing)} model(s) unavailable at {base_url}:")
        for model_id in missing:
            print(f"  - {model_id}")
        print(
            "\nEvery model step fails open, so deploying this would ship a chat "
            "whose guards all report `degraded` rather than one that crashes. "
            "Fix the ids in app/config.py (or the CADRE_MODEL_* environment), or "
            "check the API key and this account's model entitlements, then re-run."
        )
        return 1

    print("\nok: every configured model is available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
