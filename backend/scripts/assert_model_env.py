"""Fail the deploy when the models that will execute are not the ones deployed.

    aws lambda get-function-configuration --function-name cadre \
      --query 'Environment.Variables' --output json |
      python -m scripts.assert_model_env --env-json -      # from backend/

`scripts/assert_models.py` asks whether this account can invoke the models this
build is configured to call. This asks the question one step earlier, and it is
the one issue #84 was about: **will the models this build was benchmarked with
be the models that actually run?**

For weeks they were not. Terraform declared the roster a second time as
`infra/variables.tf` variables and injected them as `CADRE_MODEL_*` on the
function; environment beats code default; so a commit whose prompts had been
re-benchmarked against `ministral-3-8b` and `nemotron-nano-3-30b` deployed
cleanly and then ran on the previous roster. Nothing failed, because every
model step fails open — the pipeline renders as a working chat whatever model
answers it (KB-009). And no unit test could see it, because the map is built at
import from variables that only exist in the deployed function.

So the fix is structural: `app/config.MODEL_DEFAULTS` is the only source of
model ids, Terraform sets none of them, and this script refuses to let an image
ship into an environment that would override one. A `CADRE_MODEL_*` set by hand
for an incident is still allowed to exist — it just blocks the next deploy
until someone decides, which is the correct end for a break-glass switch.

Three separate failures, because they mean different things to whoever has to
fix them:

* **drift** — a variable that would run a model this commit did not pick;
* **unreadable** — a `CADRE_MODEL_*` no slot reads, i.e. a model setting that
  looks like configuration and does nothing (`infra/lambda.tf` has the same
  warning about names drifting out of sync with what the app reads);
* **ignored** — a blank one, which resolves to the default at runtime and is
  therefore a mistake that changes nothing until the day someone fills it in.

Takes the environment as data rather than reaching for AWS, so every decision
is provable in the unit suite. Exits 0 when the environment cannot change what
this build runs, 1 naming exactly what to remove.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping

from app import config

Ids = str | list[str]


def _render(value: Ids) -> str:
    return ",".join(value) if isinstance(value, list) else value


def drift(env: Mapping[str, str]) -> dict[str, tuple[Ids, Ids]]:
    """Slots this environment would move off the id this build expects."""
    return config.model_overrides(env)


def unreadable(env: Mapping[str, str]) -> list[str]:
    """`CADRE_MODEL_*` variables no slot in `app.config` reads."""
    known = set(config.MODEL_ENV_VARS.values())
    return sorted(
        name for name in env if name.startswith("CADRE_MODEL_") and name not in known
    )


def ignored(env: Mapping[str, str]) -> list[str]:
    """Model variables that are set but blank — they configure nothing."""
    return sorted(
        name
        for name in config.MODEL_ENV_VARS.values()
        if name in env and not env[name].strip()
    )


def main(env: Mapping[str, str], source: str = "the target environment") -> int:
    print(f"Checking the model environment of {source}")
    print(f"  code under deploy: {len(config.MODEL_DEFAULTS)} slots in app/config.py")

    drifted = drift(env)
    orphans = unreadable(env)
    blanks = ignored(env)

    for slot in sorted(config.MODEL_DEFAULTS):
        expected = _render(config.MODEL_DEFAULTS[slot])
        if slot in drifted:
            print(f"  [DRIFT] {slot}: would run {_render(drifted[slot][1])}")
            print(f"          this build expects {expected}")
        else:
            print(f"  [ok  ] {slot}: {expected}")

    if not (drifted or orphans or blanks):
        print("\nok: this environment runs exactly the models this build ships with.")
        return 0

    print("\nFAILED: the deployed code and the executing models would disagree.")

    for slot, (expected, effective) in sorted(drifted.items()):
        print(
            f"  - {config.MODEL_ENV_VARS[slot]}={_render(effective)} overrides "
            f"{slot}, which this build benchmarked as {_render(expected)}"
        )
    for name in orphans:
        print(f"  - {name} is set but no slot in app/config.py reads it")
    for name in blanks:
        print(f"  - {name} is set but blank; it configures nothing")

    print(
        "\nModel ids ship with the code they were measured against (issue #84): "
        "Terraform sets no CADRE_MODEL_* variable, so anything here is a manual "
        "break-glass override. Remove it from the function — "
        "`aws lambda update-function-configuration`, or a `terraform apply`, "
        "which no longer manages these keys — and re-run. Every model step fails "
        "open, so deploying past this would ship a chat that looks healthy while "
        "running models nobody benchmarked against these prompts."
    )
    return 1


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-json",
        required=True,
        help="File holding the target's environment as a JSON object, or '-' for stdin",
    )
    parser.add_argument(
        "--source",
        default="the target environment",
        help="What is being checked, for the log line",
    )
    args = parser.parse_args()

    raw = sys.stdin.read() if args.env_json == "-" else open(args.env_json).read()
    try:
        env = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        # An unreadable environment is not an empty one. Guessing here would
        # turn "the describe call failed" into a green gate.
        print(f"FAILED: could not parse the environment as JSON ({exc})")
        return 1
    if env is None:
        env = {}
    if not isinstance(env, dict):
        print("FAILED: expected a JSON object of environment variables")
        return 1

    return main({str(k): str(v) for k, v in env.items()}, args.source)


if __name__ == "__main__":
    sys.exit(_cli())
