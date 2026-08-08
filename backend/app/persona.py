"""Who the assistant is, and the vetted baseline of what it may claim.

This file is the product. Everything downstream of it — rails, streaming,
tracing — exists to make what is written here safe to put in front of a
stranger, so it is deliberately reviewable on its own: the prompt text lives in
`app/prompts/` as plain text files, one concern per file, with Python's
`str.format()` for the few parameterised spots.

Two constraints shape the wording:

* **The baseline is a ceiling, not a floor.** Retrieval lands in Phase 3; until
  then the model has no source but this file, so the prompt has to be explicit
  that anything absent from it is unknown rather than inferable. A model that
  guesses a price is worse than one that declines to.
* **Answers are short because the turn is capped.** CloudFront cuts the origin
  response at 60s (KB-004) and `config.BRAIN_MAX_TOKENS` caps generation to
  match. A prompt that invites an essay produces answers that get truncated
  mid-sentence, so brevity is instructed here as well as bounded there.

`config` re-exports `GREETING`, `SUGGESTIONS` and `CONTACT_URL` from this
module rather than restating them: the copy a visitor reads first and the
persona that has to answer for it must not be able to drift apart. The
dependency runs one way — persona knows nothing about config.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load(name: str, **kwargs: str) -> str:
    """Return the contents of `app/prompts/{name}.txt`, with `{placeholders}`
    filled from `kwargs`.

    Each file is read exactly once at import so a missing-file error is a
    startup crash, not a mid-turn surprise.
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    text = path.read_text(encoding="utf-8")
    if kwargs:
        text = text.format(**kwargs)
    return text.rstrip("\n")


CONTACT_URL = "https://www.cadreai.com/contact"

# Where "can you prove it?" goes. Cadre AI publishes the case studies; this
# assistant has not read them, so the link is the whole answer — a summary of
# one would be an invented result.
CASE_STUDIES_URL = "https://www.cadreai.com/case-studies"

_BASELINE = _load("baseline")

SYSTEM_PROMPT = _load(
    "system",
    baseline=_BASELINE,
    contact_url=CONTACT_URL,
    case_studies_url=CASE_STUDIES_URL,
)

# Scope text for the topic classifier. Separate from SYSTEM_PROMPT on purpose:
# the classifier needs the boundary of the subject matter, not the persona's
# manners, and feeding it the full prompt would spend tokens on rules it has
# no way to act on.
TOPIC_SCOPE = _load("topic_scope")

GREETING = "Ask me about Cadre AI — what we do, who we work with, and how to get started."

# Served by `/config` and rendered as the page's suggestion chips. Every chip
# has to earn a real answer from the baseline above: a chip the assistant
# refuses is the worst possible first impression.
SUGGESTIONS = [
    "What does Cadre AI do?",
    "How do I book a call with an AI strategist?",
    "What is the AI Maturity Index?",
    "How does Cadre AI choose LLMs and handle data security?",
]
