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

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from app import config, llm, persona, tracing
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


NO_VERDICT = "no_verdict"


@dataclass
class JudgeRun:
    """One judge call, plus the trace observation still open on it.

    The verdict is parsed here but the *meaning* of the verdict — which detail
    string it maps to — belongs to each step, so the caller closes the
    observation with `finish()`. That split is why the raw text and the parsed
    label end up on the same observation without `_judge` having to know what
    step it is serving.
    """

    label: str | None
    raw: str
    generation: object

    def finish(self, verdict: str, detail: str | None = None) -> None:
        """Close the observation with the verdict in readable form.

        The raw text rides along (truncated) because `_label`'s
        last-match-wins parse is subtle enough to have its own KB entry family
        (KB-011) — "did the parser read the model right" is only answerable
        with the model's own words next to the label they produced.
        """
        self.generation.finish(
            output={
                "raw": tracing.truncate(self.raw),
                "verdict": verdict,
                # Literals over nulls: Langfuse drops null-valued keys, and a
                # missing `detail` must not read as broken instrumentation.
                "detail": detail or "none",
            }
        )


async def _judge(
    model_id: str,
    system: str,
    user: str,
    allowed: dict[str, str],
    *,
    step: str,
    metadata: dict | None = None,
) -> JudgeRun:
    """Ask `model_id` for a verdict, on the trace.

    Returns the parsed label (None when the model answered something that is
    not one) together with the still-open generation. Raises only if the call
    itself failed — telling those two apart is what lets `classify_topic`
    decide whether walking to a fallback could possibly help, and it is the
    same distinction the trace has to carry: an outage and an unparseable
    answer are both `degraded` on the wire and must not be on the trace.

    The generation is parented ambiently, so no trace id is threaded through
    this signature and `models.py` stays monkeypatchable in one line.
    """
    generation = tracing.start_generation(
        step,
        model_id,
        params={
            "max_tokens": config.JUDGE_MAX_TOKENS,
            "temperature": config.JUDGE_TEMPERATURE,
        },
        metadata=metadata,
    )
    try:
        raw = await llm.chat(
            model_id,
            system,
            [{"role": "user", "content": user}],
            max_tokens=config.JUDGE_MAX_TOKENS,
            temperature=config.JUDGE_TEMPERATURE,
            generation=generation,
        )
    except Exception as exc:
        generation.fail(exc)
        raise

    label = _label(raw, allowed)
    if label is None:
        # Not an outage: the model answered, it just did not answer with a
        # verdict. `detail:"degraded"` cannot tell those apart; this can.
        generation.note(degraded_reason=NO_VERDICT)
    return JudgeRun(label, raw, generation)


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
        run = await _judge(
            config.MODEL_VALIDATE,
            _VALIDATE_SYSTEM,
            state.get("message", ""),
            {"pass": "pass", "fail": "fail"},
            step="validate_input",
        )
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        log.warning("validate_llm failed, passing degraded", exc_info=True)
        return Verdict("pass", DEGRADED)

    if run.label is None:
        log.warning("validate_llm returned no verdict, passing degraded")
        run.finish("pass", DEGRADED)
        return Verdict("pass", DEGRADED)
    detail = "invalid" if run.label == "fail" else None
    run.finish(run.label, detail)
    return Verdict(run.label, detail)


# --------------------------------------------------------------------------
# injection check
# --------------------------------------------------------------------------

_INJECTION_SYSTEM = _load("injection_check")


async def judge_injection(state: ConversationState) -> Verdict:
    try:
        run = await _judge(
            config.MODEL_INJECTION,
            _INJECTION_SYSTEM,
            state.get("message", ""),
            {"pass": "pass", "fail": "fail"},
            step="injection_check",
        )
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        log.warning("judge_injection failed, passing degraded", exc_info=True)
        return Verdict("pass", DEGRADED)

    if run.label is None:
        log.warning("judge_injection returned no verdict, passing degraded")
        run.finish("pass", DEGRADED)
        return Verdict("pass", DEGRADED)
    detail = "injection" if run.label == "fail" else None
    run.finish(run.label, detail)
    return Verdict(run.label, detail)


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

    **Every attempt is its own generation**, and that is the point of the
    `fallback_index` metadata. This loop used to discard `model_id`, so a trace
    named the configured primary no matter which model actually answered — and
    a trace claiming `mistral.ministral-3-8b-instruct` answered when
    `zai.glm-4.7-flash` did is worse than no data. Errored attempts end at
    `level=ERROR` with the exception class; the effective model is then not
    inferred from anything, it is simply the generation that has output.
    """
    conversation = _conversation(state)

    for index, model_id in enumerate(
        (config.MODEL_TOPIC, *config.MODEL_TOPIC_FALLBACKS)
    ):
        try:
            run = await _judge(
                model_id,
                _TOPIC_SYSTEM,
                conversation,
                _TOPIC_LABELS,
                step="topic_classifier",
                metadata={"fallback_index": index},
            )
        except Exception:  # noqa: BLE001 - try the next model, then fail open
            log.warning("classify_topic: %s failed, trying next", model_id, exc_info=True)
            continue

        if run.label is None:
            log.warning("classify_topic: %s returned no verdict, passing degraded", model_id)
            run.finish("in_scope", DEGRADED)
            return Verdict("in_scope", DEGRADED)
        run.finish(run.label)
        return Verdict(run.label)

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

    generation = tracing.start_generation(
        "condense",
        config.MODEL_CONDENSE,
        params={
            "max_tokens": config.CONDENSE_MAX_TOKENS,
            "temperature": config.JUDGE_TEMPERATURE,
        },
    )
    try:
        raw = await llm.chat(
            config.MODEL_CONDENSE,
            _CONDENSE_SYSTEM,
            [{"role": "user", "content": _conversation(state)}],
            max_tokens=config.CONDENSE_MAX_TOKENS,
            temperature=config.JUDGE_TEMPERATURE,
            generation=generation,
        )
    except Exception as exc:  # noqa: BLE001 - fail open, see docstring
        log.warning("condense_query failed, embedding the raw message", exc_info=True)
        generation.fail(exc)
        return message

    query = _plausible_query(raw)
    if query is None:
        # A `_plausible_query` rejection is invisible today: the turn simply
        # retrieves on the visitor's own words and nothing says why.
        log.warning("condense_query returned no usable query, embedding the raw message")
        generation.finish(
            output={"raw": tracing.truncate(raw), "kept_query": message},
            metadata={"degraded_reason": "implausible_rewrite", "condense_used": False},
        )
        return message

    generation.finish(
        output={"raw": tracing.truncate(raw), "kept_query": query},
        metadata={"condense_used": True},
    )
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
            # Named down to the pattern, because "which regex retracted this"
            # is a different fix from "the guard model retracted this", and
            # `pii` alone cannot tell an over-eager phone pattern from a real
            # email leak (trace-design.md §4.3). `guard_output` coarsens this
            # back to `pii` for the wire — the granularity is trace-only.
            return f"pii:{name}"
    return None


# Loaded raw and formatted per turn: the guard must judge the answer against
# the same retrieved passages the brain wrote from (issue #70 — judging a
# grounded answer against the baseline scope alone is what retracted ten
# correct fact-dense answers). The sources block sits mid-template so the
# one-word verdict instruction stays last either way (KB-011).
_GUARD_TEMPLATE = _load("output_safety")
_GUARD_SOURCES_TEMPLATE = _load("output_safety_context")


def _guard_system(context: str | None) -> str:
    """The guard prompt, with the turn's retrieved passages if there are any.

    With no context the sources section collapses to nothing and the prompt
    is the baseline-scope gate exactly as before — a KB-less turn is judged
    the way a KB-less turn always was.
    """
    sources_section = ""
    if context and context.strip():
        sources_section = "\n" + _GUARD_SOURCES_TEMPLATE.format(sources=context) + "\n"
    return _GUARD_TEMPLATE.format(
        topic_scope=persona.TOPIC_SCOPE, sources_section=sources_section
    )


NO_SCRUB_RULE = "none"


async def guard_output(state: ConversationState) -> Verdict:
    """Deterministic scrub first, then the guard model on the complete answer.

    This is the step incident 4 could not attribute: ~10 factually correct
    answers were retracted and the trace said only "the guard said fail".
    Three fields fix that, and all three are trace-only — the wire is
    unchanged.

    * **`scrub_rule`** separates the deterministic half from the model half. A
      regex retraction and a model retraction have completely different fix
      paths (tune a pattern vs tune a prompt) and are one `fail` today.
    * **the guard's raw output**, so "what did the guard actually say" is a
      field rather than an archaeology project. The #70 fix was designed blind
      for want of exactly this.
    * **`saw_context`**, the precise mechanism of incident 4: whether the guard
      judged the answer against the retrieved passages or the baseline alone.
    """
    answer = state.get("answer", "") or ""
    context = state.get("context")
    saw_context = bool(context and str(context).strip())

    scrubbed = scrub_failure(answer)
    if scrubbed:
        # No point paying for a judgement on text that is already disqualified
        # — but the step still owes the trace an observation saying so, or a
        # regex retraction is indistinguishable from a model one.
        generation = tracing.start_generation(
            "output_safety",
            config.MODEL_GUARD,
            metadata={"scrub_rule": scrubbed, "saw_context": saw_context},
        )
        generation.finish(
            output={"raw": "", "verdict": "fail", "detail": scrubbed},
            metadata={"guard_model_ran": False},
        )
        # The wire keeps the coarse value: `pii:email` on a public state event
        # is a map for getting around the check, which `REFUSAL_TEXTS` already
        # refuses to hand out.
        return Verdict("fail", scrubbed.split(":")[0])

    try:
        run = await _judge(
            config.MODEL_GUARD,
            _guard_system(context),
            answer,
            {"pass": "pass", "fail": "fail"},
            step="output_safety",
            metadata={
                "scrub_rule": NO_SCRUB_RULE,
                "saw_context": saw_context,
                "guard_model_ran": True,
            },
        )
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        log.warning("guard_output failed, passing degraded", exc_info=True)
        return Verdict("pass", DEGRADED)

    if run.label is None:
        log.warning("guard_output returned no verdict, passing degraded")
        run.finish("pass", DEGRADED)
        return Verdict("pass", DEGRADED)
    detail = "unsafe_output" if run.label == "fail" else None
    run.finish(run.label, detail)
    return Verdict(run.label, detail)


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

    The generation is ended in a `finally`, so a turn that dies mid-answer
    still ships a complete (ERROR-level) observation rather than an unended
    one — and, critically, it is ended *before* `finalize_trace` flushes.

    Its input names the persona by **file and content hash**, never by text.
    The prompts are versioned in `app/prompts/*.txt`, so a hash identifies the
    exact version without a public trace becoming the canonical leak of a
    prompt change that has not shipped yet.
    """
    context = state.get("context")
    # With no retrieved context this is `persona.SYSTEM_PROMPT` byte for byte,
    # so a turn where the KB was skipped is provably the turn the bot answered
    # before Phase 3.
    system = persona.system_prompt(context)

    generation = tracing.start_generation(
        "brain",
        config.MODEL_BRAIN,
        params={
            "max_tokens": config.BRAIN_MAX_TOKENS,
            "temperature": config.BRAIN_TEMPERATURE,
        },
        input={
            "system_prompt_file": "system.txt",
            "system_prompt_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
            "context_chars": len(context or ""),
            "history_turns": len(state.get("history", [])),
            "message_chars": len(state.get("message", "")),
        },
    )
    chars = 0
    try:
        async for text in llm.chat_stream(
            config.MODEL_BRAIN,
            system,
            _messages(state),
            max_tokens=config.BRAIN_MAX_TOKENS,
            temperature=config.BRAIN_TEMPERATURE,
            generation=generation,
        ):
            if text:
                chars += len(text)
                yield text
    except Exception as exc:
        generation.fail(exc)
        raise
    finally:
        # The answer text itself is on the trace root's output already; a copy
        # here would be the same fact twice (design principle 2).
        generation.finish(output={"answer_chars": chars})
