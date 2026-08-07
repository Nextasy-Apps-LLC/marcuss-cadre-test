import { useCallback, useRef, useState } from "react";

import { buildHistory } from "./history";
import { readSse, sha256Hex } from "./sse";
import {
  applyAborted,
  applyDone,
  applyError,
  applyState,
  applyStreamLost,
  applyToken,
  freshTurn,
  OFFLINE_TEXT,
  type TurnState,
} from "./turnReducer";
import type { ChatMessage, DoneEvent, StateEvent, StepState, TokenEvent } from "../types";

const CONVERSATION_KEY = "cadre_conversation_id";

/**
 * Same-origin in production — CloudFront routes /ask to the Lambda origin and
 * everything else to S3, so the page and the API share a hostname and the
 * browser never issues a CORS preflight ahead of the stream.
 */
const ENDPOINT = "/ask";

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
  steps: StepState[];
  busy: boolean;
  send: (text: string) => Promise<void>;
  stop: () => void;
  reset: () => void;
}

export function useCadreChat(greeting: string): CadreChat {
  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: "greeting", who: "system", text: greeting, status: "done" },
  ]);
  const [steps, setSteps] = useState<StepState[]>(() => freshTurn().steps);
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
    setSteps(freshTurn().steps);
  }, [greeting]);

  const send = useCallback(
    async (text: string) => {
      // Guard programmatic callers; the UI already disables its own controls.
      if (busy) return;

      const turnId = crypto.randomUUID();
      const replyId = `${turnId}-reply`;

      setBusy(true);
      let turn: TurnState = freshTurn();
      setSteps(turn.steps);
      setMessages((prev) => [
        ...prev,
        { id: turnId, who: "you", text, status: "done" },
        { id: replyId, who: "cadre", text: "", status: "pending" },
      ]);

      const controller = new AbortController();
      abortRef.current = controller;

      const publish = () => {
        patch(replyId, {
          text: turn.replyText,
          status: turn.replyStatus,
          outcome: turn.replyOutcome,
        });
      };

      try {
        const body = JSON.stringify({
          conversation_id: conversationId(),
          message: text,
          history: buildHistory(messages),
        });
        const response = await fetch(ENDPOINT, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            accept: "text/event-stream",
            // Lambda Function URLs behind CloudFront OAC reject POSTs whose
            // payload the viewer has not hashed: CloudFront signs the request
            // with this value, and without it Lambda answers 403 "signature
            // does not match". GET is exempt; every POST must carry it.
            "x-amz-content-sha256": await sha256Hex(body),
          },
          body,
          signal: controller.signal,
        });

        if (!response.ok || !response.body) {
          throw new Error(`bad response: ${response.status}`);
        }

        for await (const message of readSse(response.body, controller.signal)) {
          if (message.event === "state") {
            turn = applyState(turn, JSON.parse(message.data) as StateEvent);
            setSteps(turn.steps);
          } else if (message.event === "token") {
            turn = applyToken(turn, JSON.parse(message.data) as TokenEvent);
            publish();
          } else if (message.event === "done") {
            turn = applyDone(turn, JSON.parse(message.data) as DoneEvent);
            publish();
          } else if (message.event === "error") {
            turn = applyError(turn);
            publish();
          }
        }
      } catch {
        if (controller.signal.aborted) {
          turn = applyAborted(turn);
        } else {
          turn = { ...turn, replyText: OFFLINE_TEXT, replyStatus: "error" };
        }
        publish();
      } finally {
        // Stream ended without `done` and without an explicit stop: the
        // connection dropped. Any step still pending has an unknown outcome —
        // amber `lost`, not red. We genuinely do not know what it would have
        // said.
        if (!turn.sawDone && !controller.signal.aborted) {
          turn = applyStreamLost(turn);
          setSteps(turn.steps);
          publish();
        }
        abortRef.current = null;
        setBusy(false);
      }
    },
    // `messages` is read (via `buildHistory`) at the top of the function,
    // before the new turn is appended — that ordering is what excludes the
    // in-flight turn from its own history without any special-casing.
    [busy, messages, patch],
  );

  return { messages, steps, busy, send, stop, reset };
}
