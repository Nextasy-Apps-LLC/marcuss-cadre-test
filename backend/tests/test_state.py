"""`initial_state`'s server-side history budget.

The client (`web/src/lib/history.ts`) applies the same 10-turn/8000-char
budget before it ever sends a request, but the server must never trust that —
a broken or malicious client can send an oversized `history` regardless, and
the backend has to enforce its own cap rather than believing the wire. An
oversized history is truncated, not refused: it is the server's job to keep
the turn inside the CloudFront/Bedrock budget (KB-004), not to punish the
visitor for it.
"""

from __future__ import annotations

from app.graph.state import MAX_HISTORY_CHARS, MAX_HISTORY_TURNS, initial_state


def turn(role: str, text: str) -> dict:
    return {"role": role, "text": text}


class TestHistoryBudget:
    def test_budget_constants_match_the_spec(self):
        assert MAX_HISTORY_TURNS == 10
        assert MAX_HISTORY_CHARS == 8000

    def test_a_short_history_passes_through_unchanged(self):
        history = [turn("user", "hi"), turn("assistant", "hello")]
        state = initial_state("follow up", history, "abcdefgh")
        assert state["history"] == history

    def test_more_than_ten_turns_is_truncated_to_the_most_recent_ten(self):
        history = [turn("user", f"question {i}") for i in range(20)]
        state = initial_state("follow up", history, "abcdefgh")
        assert len(state["history"]) == 10
        assert state["history"][0]["text"] == "question 10"
        assert state["history"][-1]["text"] == "question 19"

    def test_oversized_text_drops_the_oldest_turns_until_within_budget(self):
        history = [
            turn("user", "a" * 3000),
            turn("user", "b" * 3000),
            turn("user", "c" * 3000),
        ]
        state = initial_state("follow up", history, "abcdefgh")
        total = sum(len(t["text"]) for t in state["history"])
        assert total <= MAX_HISTORY_CHARS
        assert state["history"][0]["text"][0] == "b"
        assert state["history"][-1]["text"][0] == "c"

    def test_the_single_most_recent_turn_is_kept_even_if_it_alone_exceeds_the_budget(self):
        history = [turn("user", "z" * 9000)]
        state = initial_state("follow up", history, "abcdefgh")
        assert len(state["history"]) == 1
        assert len(state["history"][0]["text"]) == 9000

    def test_the_turn_cap_applies_before_the_char_trim(self):
        history = [turn("user", f"msg {i}") for i in range(11)]
        state = initial_state("follow up", history, "abcdefgh")
        assert len(state["history"]) == 10
        assert state["history"][0]["text"] == "msg 1"

    def test_an_oversized_history_is_truncated_not_refused(self):
        # Truncation happens in initial_state, upstream of validate_input —
        # there is no refusal path for "history too long", only a trim.
        history = [turn("user", f"question {i}") for i in range(50)]
        state = initial_state("follow up", history, "abcdefgh")
        assert state["outcome"] == "answered"
        assert len(state["history"]) <= MAX_HISTORY_TURNS

    def test_empty_history_stays_empty(self):
        state = initial_state("hello", [], "abcdefgh")
        assert state["history"] == []
