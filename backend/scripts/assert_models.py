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

* **The model exists** in the target region — `list-foundation-models` /
  `list-inference-profiles`. Catches typos and renames.
* **The account may invoke it** — `get-foundation-model-availability` reports
  `authorizationStatus`. A brand-new account lists every model in the catalogue
  while being authorised for none of them, so presence alone proves nothing.

The topic classifier is the one step allowed a hole: it has a fallback chain,
so the chain is satisfied by any one member. Everything else is required.

Exits 0 when the account can serve this build, non-zero naming what it cannot.
"""

from __future__ import annotations

import sys

from app import config

# Cross-region inference-profile ids are the foundation-model id with a
# geography prefix. Availability is a property of the underlying model, so the
# prefix comes off before asking about it.
_GEO_PREFIXES = ("us.", "eu.", "apac.", "global.")


def foundation_model_id(model_id: str) -> str:
    for prefix in _GEO_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix) :]
    return model_id


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
    """Every id this build may call, in the order a turn uses them."""
    ordered = [
        config.MODEL_VALIDATE,
        config.MODEL_INJECTION,
        *topic_chain(),
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


def available_models(region: str | None = None) -> set[str]:
    """The configured ids this account can actually invoke, right now.

    Scoped to the configured ids rather than the whole catalogue: the
    per-model authorisation call is what makes the answer trustworthy, and
    making ~100 of them to answer a question about six would be slow and
    rude.
    """
    import boto3  # imported here so the pure helpers stay importable offline

    region = region or config.BEDROCK_REGION
    bedrock = boto3.client("bedrock", region_name=region)

    listed = {
        summary["modelId"]
        for summary in bedrock.list_foundation_models().get("modelSummaries", [])
    }
    profiles = {
        summary["inferenceProfileId"]
        for summary in bedrock.list_inference_profiles().get("inferenceProfileSummaries", [])
        if summary.get("status") == "ACTIVE"
    }
    catalogue = listed | profiles

    available: set[str] = set()
    for model_id in set(required_models()):
        if model_id not in catalogue:
            continue
        try:
            availability = bedrock.get_foundation_model_availability(
                modelId=foundation_model_id(model_id)
            )
        except Exception as exc:  # noqa: BLE001 - reported below as unavailable
            print(f"  ! {model_id}: availability lookup failed ({exc})")
            continue
        if availability.get("authorizationStatus") == "AUTHORIZED":
            available.add(model_id)
        else:
            print(
                f"  ! {model_id}: listed but not authorised "
                f"(authorizationStatus={availability.get('authorizationStatus')}, "
                f"agreement={availability.get('agreementAvailability', {}).get('status')})"
                " — grant model access for this account"
            )
    return available


def main(available: set[str] | None = None) -> int:
    region = config.BEDROCK_REGION
    print(f"Checking Bedrock model access in {region}")

    if available is None:
        available = available_models(region)

    for model_id in required_models():
        mark = "ok  " if model_id in available else "MISS"
        print(f"  [{mark}] {model_id}")

    missing = missing_models(available)
    if missing:
        print(f"\nFAILED: {len(missing)} model(s) unavailable in {region}:")
        for model_id in missing:
            print(f"  - {model_id}")
        print(
            "\nEvery model step fails open, so deploying this would ship a chat "
            "whose guards all report `degraded` rather than one that crashes. "
            "Fix the ids in app/config.py (or the CADRE_MODEL_* environment) or "
            "grant model access, then re-run."
        )
        return 1

    print("\nok: every configured model is available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
