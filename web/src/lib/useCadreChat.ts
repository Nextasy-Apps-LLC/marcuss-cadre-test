import { useCallback, useRef, useState } from "react";

import { readSse } from "./sse";
import {
  freshRails,
  RAIL_SPECS,
  type ChatMessage,
  type DoneEvent,
  type RailEvent,
  type RailState,
} from "../types";

const CONVERSATION_KEY = "cadre_conversation_id";

/**
 * Same-origin in production — CloudFront routes /ask to the Lambda origin and
 * everything else to S3, so the page and the API share a hostname and the
 * browser never issues a CORS preflight ahead of the stream.
 */
const ENDPOINT = "/ask";

const OFFLINE_TEXT = "The chat is offline right now. Try again in a moment.";
const ERROR_TEXT = "Something went wrong. Try again in a moment.";
const REFUSED_TEXT = "Sorry — I can't answer that one.";

function conversationId(): string {
  let id = localStorage.getItem(CONVERSATION_KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(CONVERSATION_KEY, id);
  }
  return id;
}

export interface CadreChat {
  messages: ChatMessage[];
  rails: RailState[];
  totalMs: number | null;
  busy: boolean;
  send: (text: string) => Promise<void>;
  stop: () => void;
  reset: () => void;
}

export function useCadreChat(greeting: string): CadreChat {
  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: "greeting", who: "system", text: greeting, status: "done" },
  ]);
  const [rails, setRails] = useState<RailState[]>(freshRails);
  const [totalMs, setTotalMs] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const abortRef = useRef<AbortController | null>(null);

  const patch = useCallback((id: string, changes: Partial<ChatMessage>) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, ...changes } : m)),
    );
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const reset = useCallback(() => {
    localStorage.removeItem(CONVERSATION_KEY);
    setMessages([{ id: "greeting", who: "system", text: greeting, status: "done" }]);
    setRails(freshRails());
    setTotalMs(null);
  }, [greeting]);

  const send = useCallback(
    async (text: string) => {
      // Guard programmatic callers; the UI already disables its own controls.
      if (busy) return;

      const turnId = crypto.randomUUID();
      const replyId = `${turnId}-reply`;

      setBusy(true);
      setRails(freshRails());
      setTotalMs(null);
      setMessages((prev) => [
        ...prev,
        { id: turnId, who: "you", text, status: "done" },
        { id: replyId, who: "cadre", text: "", status: "pending" },
      ]);

      const controller = new AbortController();
      abortRef.current = controller;

      // Tracked so the finally-block can tell "the stream ended cleanly" from
      // "the connection died mid-turn", which are rendered very differently.
      let sawDone = false;
      let blockedIndex = -1;
      const reported = new Set<string>();
      let buffer = "";

      try {
        const response = await fetch(ENDPOINT, {
          method: "POST",
          headers: { "content-type": "application/json", accept: "text/event-stream" },
          body: JSON.stringify({ conversation_id: conversationId(), message: text }),
          signal: controller.signal,
        });

        if (!response.ok || !response.body) {
          throw new Error(`bad response: ${response.status}`);
        }

        for await (const message of readSse(response.body, controller.signal)) {
          if (message.event === "rail") {
            const event = JSON.parse(message.data) as RailEvent;
            reported.add(event.rail_id);

            // A degraded rail lets the turn continue, so it must not be
            // treated as the blocker that marks everything after it skipped.
            if (!event.passed && !event.degraded && blockedIndex === -1) {
              blockedIndex = RAIL_SPECS.findIndex((s) => s.id === event.rail_id);
            }

            setRails((prev) =>
              prev.map((rail) =>
                rail.id === event.rail_id
                  ? {
                      ...rail,
                      status: event.degraded
                        ? "degraded"
                        : event.passed
                          ? "passed"
                          : "blocked",
                      latencyMs: event.latency_ms,
                      reason: event.reason,
                    }
                  : rail,
              ),
            );
          } else if (message.event === "token") {
            const { text: chunk } = JSON.parse(message.data) as { text: string };
            buffer += chunk;
            patch(replyId, { text: buffer, status: "streaming" });
          } else if (message.event === "done") {
            const event = JSON.parse(message.data) as DoneEvent;
            sawDone = true;
            setTotalMs(event.latency_ms);

            // Rails after the blocker never ran. Mark them skipped rather
            // than leaving them spinning forever.
            if (blockedIndex !== -1) {
              setRails((prev) =>
                prev.map((rail, i) =>
                  i > blockedIndex && !reported.has(rail.id)
                    ? { ...rail, status: "skipped" }
                    : rail,
                ),
              );
            }

            // A refusal can arrive *after* tokens were streamed — the output
            // guard only sees a complete reply. Overwrite whatever is on
            // screen; the streamed text was provisional.
            patch(replyId, {
              text: event.refused ? REFUSED_TEXT : buffer,
              status: "done",
            });
          } else if (message.event === "error") {
            sawDone = true;
            patch(replyId, { text: ERROR_TEXT, status: "error" });
          }
        }
      } catch (err) {
        if (controller.signal.aborted) {
          patch(replyId, { text: buffer || "(stopped)", status: "stopped" });
        } else {
          patch(replyId, { text: OFFLINE_TEXT, status: "error" });
        }
      } finally {
        // Stream ended without a `done` and without an explicit stop: the
        // connection dropped. Any rail still pending has an unknown outcome —
        // amber, not red. We genuinely do not know what it would have said.
        if (!sawDone && !controller.signal.aborted) {
          setRails((prev) =>
            prev.map((rail) =>
              rail.status === "pending" ? { ...rail, status: "lost" } : rail,
            ),
          );
          patch(replyId, { text: buffer || OFFLINE_TEXT, status: "error" });
        }
        abortRef.current = null;
        setBusy(false);
      }
    },
    [busy, patch],
  );

  return { messages, rails, totalMs, busy, send, stop, reset };
}
