"""The model steps — Bedrock Mantle chat completions, one model per job.

Phase 1a shaped these as seams and proved the engine around them offline.
This is Phase 1b filling them, and nothing about the graph, the nodes or the
wire contract moved to accommodate it: the functions kept their signatures, so
the protocol tests that ran against monkeypatched stubs still run against the
same shapes.

Nodes call these through the module (`models.judge_injection(...)`), never by
importing the name — that is what keeps them patchable in one line.

Verdict vocabulary:
    validate_llm    -> "pass" | "fail"
    judge_injection -> "pass" | "fail"
    classify_topic  -> "in_scope" | "off_topic" | "needs_human"
    guard_output    -> "pass" | "fail"
    stream_reply    -> async iterator of answer fragments

## The fail-open policy, stated once

Every judge here returns `Verdict("pass", "degraded")` — or, for the
classifier, `Verdict("in_scope", "degraded")` — when it cannot get a real
answer. Two distinct situations produce that:

* **The model errored.** Bedrock is down, the id is wrong, the role lacks
  `bedrock:InvokeModel`. A visitor should not be refused because of any of
  those.
* **The model answered something that is not a verdict.** "I'm not sure",
  an empty string, a paragraph of reasoning. Guessing which way it leant
  would be inventing a decision the model declined to make.

`detail:"degraded"` is not decoration. It is the only thing separating "the
guard approved this" from "no guard ran" on the wire, and the client renders
it amber for exactly that reason — a fail-open guard that reported green
would make a misconfigured model indistinguishable from a healthy turn
(KB-009).

`brain` is the deliberate exception: there is no answer to degrade to, so
`stream_reply` lets the failure propagate and become a terminal `error`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from app import config, llm, persona
from app.graph.state import ConversationState

log = logging.getLogger("cadre.models")

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load(name: str, **kwargs: str) -> str:
    """Return `app/prompts/{name}.txt`, with placeholders filled."""
    text = (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    if kwargs:
        text = text.format(**kwargs)
    return text.rstrip("\n")


@dataclass(frozen=True)
class Verdict:
    """A step's decision plus the machine-readable reason put on the wire."""

    verdict: str
    detail: str | None = None


DEGRADED = "degraded"


def _label(raw: str, allowed: dict[str, str]) -> str | None:
    """The verdict in `raw`, or None if there isn't one.

    Two decisions here, both learned from what the roster actually returns:

    * **Reasoning is stripped first.** Several Mantle models think out loud in
      `<think>…</think>` before answering, and a response truncated
      mid-thought has no verdict at all — `strip_reasoning` returns empty for
      that case, so a cut-off monologue degrades instead of being mined.
    * **The last match wins, not the first.** A model that reasons in plain
      prose says things like "this could pass, but it overrides instructions,
      so fail". Taking the first label inverts the decision. The conclusion is
      at the end.
    """
    text = llm.strip_reasoning(raw).lower()
    if not text:
        return None

    # Labels may arrive with `_`, a space or a hyphen between words. Built
    # from the raw token rather than `re.escape(token)`: escaping a token
    # first and then hunting for `\_` in the result is a no-op on Python
    # 3.7+, where `re.escape` stopped escaping `_` — this alternation must
    # fire post-escape, so it's built pre-escape instead. Every key in
    # `allowed` is `[a-z_]` only (see call sites), so no other character
    # needs escaping.
    pattern = "|".join(
        token.replace("_", "[_ -]") for token in sorted(allowed, key=len, reverse=True)
    )
    matches = re.findall(rf"\b(?:{pattern})\b", text)
    if not matches:
        return None
    return allowed[matches[-1].replace(" ", "_").replace("-", "_")]


async def _judge(
    model_id: str,
    system: str,
    user: str,
    allowed: dict[str, str],
) -> str | None:
    """Ask `model_id` for a verdict.

    Returns the parsed label, or None when the model answered something that
    is not one. Raises only if the call itself failed — telling those two
    apart is what lets `classify_topic` decide whether walking to a fallback
    could possibly help.
    """
    raw = await llm.chat(
        model_id,
        system,
        [{"role": "user", "content": user}],
        max_tokens=config.JUDGE_MAX_TOKENS,
        temperature=config.JUDGE_TEMPERATURE,
    )
    return _label(raw, allowed)


# --------------------------------------------------------------------------
# validate_input, second half
# --------------------------------------------------------------------------

_VALIDATE_SYSTEM = _load("validate_input")


async def validate_llm(state: ConversationState) -> Verdict:
    """The model-backed half of input validation.

    Deliberately narrow: the deterministic checks in `nodes.validate_input`
    already rejected empty, over-long, control-character and malformed
    payloads before this ran. All that is left is "is this a message at all",
    which is why the smallest model in the roster does it.
    """
    try:
        label = await _judge(
            config.MODEL_VALIDATE,
            _VALIDATE_SYSTEM,
            state.get("message", ""),
            {"pass": "pass", "fail": "fail"},
        )
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        log.warning("validate_llm failed, passing degraded", exc_info=True)
        return Verdict("pass", DEGRADED)

    if label is None:
        log.warning("validate_llm returned no verdict, passing degraded")
        return Verdict("pass", DEGRADED)
    return Verdict(label, "invalid" if label == "fail" else None)


# --------------------------------------------------------------------------
# injection check
# --------------------------------------------------------------------------

_INJECTION_SYSTEM = _load("injection_check")


async def judge_injection(state: ConversationState) -> Verdict:
    try:
        label = await _judge(
            config.MODEL_INJECTION,
            _INJECTION_SYSTEM,
            state.get("message", ""),
            {"pass": "pass", "fail": "fail"},
        )
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        log.warning("judge_injection failed, passing degraded", exc_info=True)
        return Verdict("pass", DEGRADED)

    if label is None:
        log.warning("judge_injection returned no verdict, passing degraded")
        return Verdict("pass", DEGRADED)
    return Verdict(label, "injection" if label == "fail" else None)


# --------------------------------------------------------------------------
# topic classifier
# --------------------------------------------------------------------------

_TOPIC_SYSTEM = _load("topic_classifier", topic_scope=persona.TOPIC_SCOPE)

_TOPIC_LABELS = {
    "in_scope": "in_scope",
    "inscope": "in_scope",
    "off_topic": "off_topic",
    "offtopic": "off_topic",
    "needs_human": "needs_human",
    "needshuman": "needs_human",
}


def _conversation(state: ConversationState) -> str:
    """The turn, with whatever history the client sent for context.

    A follow-up like "how much does that cost?" is only classifiable against
    what came before it; without history it reads as off-topic.
    """
    lines = [
        f"{turn.get('role', 'user')}: {turn.get('text', '')}"
        for turn in state.get("history", [])
    ]
    lines.append(f"user: {state.get('message', '')}")
    return "\n".join(lines)


async def classify_topic(state: ConversationState) -> Verdict:
    """Nemotron first, then the fallback chain, then a degraded pass.

    The chain is walked on *errors only*. A model that answered — even
    unparseably — is a model that is up, and asking a second one would spend
    another slice of the 55s turn budget to re-ask a question the output guard
    already backstops.
    """
    conversation = _conversation(state)

    for model_id in (config.MODEL_TOPIC, *config.MODEL_TOPIC_FALLBACKS):
        try:
            label = await _judge(model_id, _TOPIC_SYSTEM, conversation, _TOPIC_LABELS)
        except Exception:  # noqa: BLE001 - try the next model, then fail open
            log.warning("classify_topic: %s failed, trying next", model_id, exc_info=True)
            continue

        if label is None:
            log.warning("classify_topic: %s returned no verdict, passing degraded", model_id)
            return Verdict("in_scope", DEGRADED)
        return Verdict(label)

    log.warning("classify_topic: whole fallback chain failed, passing degraded")
    return Verdict("in_scope", DEGRADED)


# --------------------------------------------------------------------------
# query condensing, for `retrieve`
# --------------------------------------------------------------------------

_CONDENSE_SYSTEM = _load("condense")


def _plausible_query(text: str) -> str | None:
    """`text` if it could be a search query, else None.

    The condenser reads free text back from a model, so it gets the same
    treatment as a judge (KB-011): reasoning is stripped first, and an
    *unclosed* `<think>` — a monologue truncated by the token cap — leaves
    nothing at all rather than a fragment. What survives is then sanity
    checked, because a rewrite that is empty, or long enough to be an answer
    rather than a query, is a condenser that did the wrong job.
    """
    text = llm.strip_reasoning(text)
    if not text:
        return None
    # Models like to explain first and conclude last; the query is the final
    # non-empty line, not the preamble in front of it.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    query = lines[-1].strip().strip('"').strip("'").strip()
    if not query or len(query) > config.CONDENSE_MAX_CHARS:
        return None
    return query


async def condense_query(state: ConversationState) -> str:
    """The visitor's message, rewritten to stand on its own.

    "How much does that cost?" retrieves nothing on its own; against the
    previous turn it is "Cadre AI Maturity Index pricing". That is the whole
    job, and it is why this call exists at all.

    Two boundaries:

    * **No history, no call.** A first message is already standalone, and
      spending part of the 60s turn budget (KB-004) to confirm it would be a
      pure loss. `nodes.retrieve` enforces the same rule at the call site so
      the seam can be asserted un-called; this check is the backstop.
    * **Fail-open to the visitor's own words.** An outage, an empty answer, a
      truncated monologue or an answer that is not a query all fall back to
      `state["message"]`. A bad rewrite is worse than no rewrite: it retrieves
      confidently for a question nobody asked.
    """
    message = state.get("message", "")
    if not state.get("history"):
        return message

    try:
        raw = await llm.chat(
            config.MODEL_CONDENSE,
            _CONDENSE_SYSTEM,
            [{"role": "user", "content": _conversation(state)}],
            max_tokens=config.CONDENSE_MAX_TOKENS,
            temperature=config.JUDGE_TEMPERATURE,
        )
    except Exception:  # noqa: BLE001 - fail open, see docstring
        log.warning("condense_query failed, embedding the raw message", exc_info=True)
        return message

    query = _plausible_query(raw)
    if query is None:
        log.warning("condense_query returned no usable query, embedding the raw message")
        return message
    return query


# --------------------------------------------------------------------------
# output safety: deterministic scrub + guard model
# --------------------------------------------------------------------------

# Anything link-shaped, so a bare `example.com/x` is caught alongside a full
# URL. Trailing sentence punctuation is not part of the host.
_URL = re.compile(r"\b(?:https?://)?([a-z0-9.-]+\.[a-z]{2,})(?:/[^\s]*)?", re.I)
_ALLOWED_HOSTS = ("cadreai.com",)

_PII = (
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # Deliberately loose: a false positive costs a refusal the visitor can
    # rephrase past, a false negative puts a phone number on a stranger's
    # screen. Requires a separator so it cannot match a plain year or price.
    ("phone", re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:\(?\d{3}\)?[\s-]){2}\d{3,4}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
)


def _is_allowed_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == allowed or host.endswith("." + allowed) for allowed in _ALLOWED_HOSTS)


def scrub_failure(answer: str) -> str | None:
    """The first deterministic reason to pull an answer back, or None.

    This is the half of output safety that has no outage mode. It runs before
    the guard model and independently of whether the guard model answers, so a
    Bedrock failure can degrade the *judgement* half of the gate without ever
    degrading this one.

    It reports rather than rewrites: the answer has already been streamed to
    the visitor by the time `output_safety` runs, so there is no edited text
    to send — `refuse` replaces the whole thing with `done.refusal_text`.
    That stream-then-retract trade-off is plan.md's, not this function's.
    """
    for host in _URL.findall(answer):
        if not _is_allowed_host(host):
            return "external_url"

    for name, pattern in _PII:
        if pattern.search(answer):
            log.info("output scrub matched %s", name)
            return "pii"
    return None


_GUARD_SYSTEM = _load("output_safety", topic_scope=persona.TOPIC_SCOPE)


async def guard_output(state: ConversationState) -> Verdict:
    """Deterministic scrub first, then Haiku on the complete answer."""
    answer = state.get("answer", "") or ""

    scrubbed = scrub_failure(answer)
    if scrubbed:
        # No point paying for a judgement on text that is already disqualified.
        return Verdict("fail", scrubbed)

    try:
        label = await _judge(
            config.MODEL_GUARD,
            _GUARD_SYSTEM,
            answer,
            {"pass": "pass", "fail": "fail"},
        )
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        log.warning("guard_output failed, passing degraded", exc_info=True)
        return Verdict("pass", DEGRADED)

    if label is None:
        log.warning("guard_output returned no verdict, passing degraded")
        return Verdict("pass", DEGRADED)
    return Verdict(label, "unsafe_output" if label == "fail" else None)


# --------------------------------------------------------------------------
# the brain
# --------------------------------------------------------------------------

_ROLES = {"assistant": "assistant", "ai": "assistant", "bot": "assistant"}


def _messages(state: ConversationState) -> list[dict[str, str]]:
    """History plus this turn, as OpenAI-shaped messages.

    The system turn is not here — `llm.chat_stream` prepends it, so the
    persona reaches every call the same way.
    """
    messages: list[dict[str, str]] = []
    for turn in state.get("history", []):
        role = _ROLES.get(str(turn.get("role", "")).lower(), "user")
        messages.append({"role": role, "content": turn.get("text", "")})
    messages.append({"role": "user", "content": state.get("message", "")})
    return messages


async def stream_reply(state: ConversationState) -> AsyncIterator[str]:
    """Answer fragments as the brain generates them.

    The only step that does not fail open: there is no answer to degrade to,
    so an exception here propagates and `/ask` turns it into a terminal
    `error` event. A brain outage is a broken turn, and saying so is more
    honest than streaming a placeholder.
    """
    async for text in llm.chat_stream(
        config.MODEL_BRAIN,
        # With no retrieved context this is `persona.SYSTEM_PROMPT` byte for
        # byte, so a turn where the KB was skipped is provably the turn the
        # bot answered before Phase 3.
        persona.system_prompt(state.get("context")),
        _messages(state),
        max_tokens=config.BRAIN_MAX_TOKENS,
        temperature=config.BRAIN_TEMPERATURE,
    ):
        if text:
            yield text
