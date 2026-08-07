"""The one place a Bedrock client is constructed.

Every LLM call in this service goes through `chat_model()` and therefore
through LangChain's `ChatBedrockConverse` — never a raw `boto3`
`bedrock-runtime` invoke (backend/CLAUDE.md). The payoff is not abstraction
for its own sake: six steps that each hand-rolled an invoke shape would be six
places to add a callback handler when Langfuse lands in Phase 2, six places
that disagree about how a block-list response is flattened, and six places to
audit when a model id turns out to need an inference profile.

Callers name a model and a token budget; nothing else about the client is
theirs to choose. That is what keeps `scripts/assert_models.py` able to state
truthfully that the ids in `app.config` are the ids this build calls.
"""

from __future__ import annotations

from langchain_aws import ChatBedrockConverse

from app import config

# Claude Opus 5 does not accept sampling parameters — `temperature`, `top_p`
# and `top_k` were removed on it. `langchain-aws` does not error on one; it
# logs `Model … does not support temperature; ignoring the provided value` and
# drops it, which means a temperature set here would be a silent no-op rather
# than a knob. The judges genuinely need `temperature=0`, so rather than let
# every call site guess, the factory refuses to send the parameter to a model
# that would discard it.
_NO_SAMPLING_PARAMS = ("claude-opus-5",)


def _accepts_temperature(model_id: str) -> bool:
    return not any(family in model_id for family in _NO_SAMPLING_PARAMS)


def chat_model(
    model_id: str,
    *,
    max_tokens: int,
    temperature: float | None = None,
    **kwargs,
) -> ChatBedrockConverse:
    """A Bedrock chat client for `model_id`, in the configured region.

    Auth is SigV4 from the ambient role — the Lambda execution role in
    production, whatever the shell has locally. There is no API key in this
    stack, which is what lets the repository be public.
    """
    params: dict = {
        "model": model_id,
        "region_name": config.BEDROCK_REGION,
        "max_tokens": max_tokens,
        **kwargs,
    }
    if temperature is not None and _accepts_temperature(model_id):
        params["temperature"] = temperature
    return ChatBedrockConverse(**params)


def text_of(content) -> str:
    """Flatten a message's `content` to plain text.

    `ChatBedrockConverse` returns a bare `str` for some models and a list of
    typed blocks (`{"type": "text", "text": …}`, reasoning blocks, tool blocks)
    for others — and which one you get depends on the model, not on the call.
    Every reader of a response goes through here so that a judge does not
    decide `[{'type': 'text', 'text': 'fail'}]` is unparseable and degrade a
    perfectly good verdict.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""
