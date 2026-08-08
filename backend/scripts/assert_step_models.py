"""Post-deploy smoke: the live service must be running what was deployed.

    python -m scripts.assert_step_models --base-url https://cadre.marcuss.pro

`scripts/assert_model_env.py` proves the intent before the image is pushed.
This proves the outcome afterwards, from the outside, by reading the running
service's own `/config` and comparing `step_models` with what the deployed
commit expects. It is the check that would have caught issue #84 on the day it
happened rather than weeks later by hand — production was serving

    injection_check: "nemotron 30b"   topic_classifier: "ministral 8b"

for steps running qwen3-32b and gemma-3-12b, and nothing anywhere disagreed
with it, because nothing anywhere was looking.

Two deliberate choices:

* **The expectation comes from `config.DEFAULT_STEP_MODELS`**, not from
  `config.STEP_MODELS`. The latter reflects whatever environment the *checker*
  is running with, and a smoke test that adopts the target's opinion — or its
  own host's — cannot fail. This is the same reason `assert_models.py` spends a
  real completion instead of trusting the catalogue.
* **Every decision is a pure function** over the payload, so the failure is
  provable offline in the unit suite. A gate whose failure path has never been
  executed is not a gate.

Scope, stated plainly (KB-007): this proves *which models are wired to which
step*. It does not prove a turn streams, and it is not a substitute for the
browser check or for the `/healthz` and page probes it runs beside.

Exits 0 when the target's labels match the deployed commit, 1 naming each step
that does not.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping

from app import config

Mismatch = tuple[str, str | None, str | None]


def expected() -> dict[str, str]:
    """The step → model labels this commit ships with."""
    return dict(config.DEFAULT_STEP_MODELS)


def mismatches(served: Mapping[str, str] | None) -> list[Mismatch]:
    """`(step, expected, served)` for every step the target gets wrong.

    `None` on either side means the step is missing there — an older image
    serving fewer steps, or a newer one serving a step this commit does not
    know about. Both are "the deploy did not land what I think it did", which
    is exactly what this exists to detect, so neither is quietly tolerated.
    """
    served = served or {}
    found: list[Mismatch] = []

    for step, label in expected().items():
        actual = served.get(step)
        if actual != label:
            found.append((step, label, actual))

    for step in served:
        if step not in expected():
            found.append((step, None, served[step]))

    return found


def check(payload: Mapping[str, object], base_url: str) -> int:
    print(f"Checking step_models at {base_url}/config")

    served = payload.get("step_models") if isinstance(payload, Mapping) else None
    if not isinstance(served, Mapping) or not served:
        # An empty or absent map is not "nothing to compare" — it is a target
        # that cannot tell you what it is running, which is a failed deploy
        # until proven otherwise.
        print("\nFAILED: the target served no step_models at all.")
        print(
            "  /config without a populated step_models map is an image that "
            "predates it — the deploy did not land, or it landed on the wrong "
            "function."
        )
        return 1

    found = mismatches({str(k): str(v) for k, v in served.items()})

    for step, label in expected().items():
        actual = served.get(step)
        mark = "ok  " if actual == label else "MISS"
        print(f"  [{mark}] {step}: {actual!r} (deployed commit expects {label!r})")

    if not found:
        print("\nok: the live service is running the models this commit ships with.")
        return 0

    print(f"\nFAILED: {len(found)} step(s) do not match the deployed commit:")
    for step, label, actual in found:
        if label is None:
            print(f"  - {step}: served {actual!r}, which this commit does not define")
        elif actual is None:
            print(f"  - {step}: missing; this commit expects {label!r}")
        else:
            print(f"  - {step}: serving {actual!r}, this commit expects {label!r}")

    print(
        "\nThe running service is not the code that was deployed. Check for a "
        "CADRE_MODEL_* override on the function (`aws lambda "
        "get-function-configuration`) — model ids ship with the code they were "
        "benchmarked against and Terraform sets none of them (issue #84) — or "
        "for a deploy that updated the image without the function picking it up."
    )
    return 1


def fetch_config(base_url: str, timeout: float = 30.0) -> dict:
    """`GET {base_url}/config`. The only impure thing in this module."""
    import httpx  # imported here so the pure helpers stay importable offline

    response = httpx.get(f"{base_url.rstrip('/')}/config", timeout=timeout)
    response.raise_for_status()
    return response.json()


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        required=True,
        help="Target to read /config from, e.g. https://cadre.marcuss.pro",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    try:
        payload = fetch_config(base_url)
    except Exception as exc:  # noqa: BLE001 - a target that cannot answer fails
        print(f"FAILED: could not read {base_url}/config ({type(exc).__name__}: {exc})")
        return 1

    return check(payload, base_url)


if __name__ == "__main__":
    sys.exit(_cli())
