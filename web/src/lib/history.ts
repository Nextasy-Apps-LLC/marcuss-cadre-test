/**
 * Builds the `history: Turn[]` the `/ask` body carries, from the transcript
 * `useCadreChat` already keeps in React state.
 *
 * Rules (issue #43):
 *  - Includes every user turn.
 *  - Includes an assistant turn only once it is `done` with an outcome of
 *    `answered` or `escalated` — a refused, errored, stopped or still
 *    in-flight (`pending`/`streaming`) reply never counts as context. The
 *    in-flight turn itself is naturally excluded too: the caller builds
 *    history from the transcript *before* appending the new user message and
 *    its pending reply.
 *  - Keeps only the most recent `MAX_HISTORY_TURNS` turns, then drops the
 *    oldest of those until the total text is at most `MAX_HISTORY_CHARS` —
 *    but never below the single most recent turn, even if that turn alone
 *    exceeds the budget; the alternative is silently sending no history at
 *    all right when the visitor is mid-follow-up.
 *
 * The backend enforces the identical budget server-side (`app/graph/state.py`
 * `MAX_HISTORY_TURNS`/`MAX_HISTORY_CHARS`) — this trim is a courtesy that
 * keeps the request small, not the only line of defense.
 */

import type { ChatMessage, Turn } from "../types";

export const MAX_HISTORY_TURNS = 10;
export const MAX_HISTORY_CHARS = 8000;

function includable(message: ChatMessage): boolean {
  if (message.who === "you") return true;
  if (message.who === "cadre") {
    return message.status === "done" && (message.outcome === "answered" || message.outcome === "escalated");
  }
  return false; // the system greeting is never conversation context
}

export function buildHistory(messages: ChatMessage[]): Turn[] {
  const turns: Turn[] = messages.filter(includable).map((message) => ({
    role: message.who === "you" ? "user" : "assistant",
    text: message.text,
  }));

  const recent = turns.slice(-MAX_HISTORY_TURNS);

  let start = 0;
  let total = recent.reduce((sum, turn) => sum + turn.text.length, 0);
  while (total > MAX_HISTORY_CHARS && start < recent.length - 1) {
    total -= recent[start]!.text.length;
    start += 1;
  }
  return recent.slice(start);
}
