import { useCallback, useEffect, useRef, useState } from "react";

import { buildHistory } from "./history";
import { readSse, sha256Hex } from "./sse";
import {
  applyAborted,
  applyDone,
  applyError,
  applyState,
  applyStreamLost,
  applyToken,
  applyTrace,
  freshTurn,
  OFFLINE_TEXT,
  type TurnState,
} from "./turnReducer";
import type { ChatMessage, DoneEvent, StateEvent, StepName, StepState, TokenEvent, TraceEvent, TurnSummary } from "../types";

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
  /**
   * The current turn's aggregate, set once its `done` event arrives with a
   * `summary` (issue #109). `undefined` while a turn runs, when tracing was
   * down, and after a turn that ended without `done`.
   */
  summary?: TurnSummary;
  busy: boolean;
  send: (text: string) => Promise<void>;
  stop: () => void;
  reset: () => void;
}

export function useCadreChat(greeting: string, stepModels?: Partial<Record<StepName, string>>): CadreChat {
  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: "greeting", who: "system", text: greeting, status: "done" },
  ]);
  const [steps, setSteps] = useState<StepState[]>(() => freshTurn(stepModels).steps);
  const [summary, setSummary] = useState<TurnSummary | undefined>(undefined);
  const [busy, setBusy] = useState(false);

  const abortRef = useRef<AbortController | null>(null);

  // `messages` is seeded synchronously at mount, before `/config` has
  // answered — so the first paint necessarily uses `App`'s FALLBACK greeting.
  // Without this, nothing ever re-syncs it and the visitor sees that fallback
  // for the whole session, never the real server-side greeting (issue #97).
  //
  // Patches the greeting row IN PLACE rather than re-seeding `messages`: the
  // transcript is also the conversation-history source (`buildHistory`), so
  // replacing the array wholesale when `/config` lands would silently drop
  // history mid-conversation and, with it, the condensed query that only
  // appears on a follow-up. Returning `prev` untouched when there is nothing
  // to do keeps a `greeting`-identity change from re-rendering the transcript
  // for nothing.
  useEffect(() => {
    setMessages((prev) => {
      const index = prev.findIndex((m) => m.id === "greeting");
      if (index === -1 || prev[index]!.text === greeting) return prev;
      const next = [...prev];
      next[index] = { ...next[index]!, text: greeting };
      return next;
    });
  }, [greeting]);

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
    setSteps(freshTurn(stepModels).steps);
    setSummary(undefined);
  }, [greeting, stepModels]);

  const send = useCallback(
    async (text: string) => {
      // Guard programmatic callers; the UI already disables its own controls.
      if (busy) return;

      const turnId = crypto.randomUUID();
      const replyId = `${turnId}-reply`;

      setBusy(true);
      let turn: TurnState = freshTurn(stepModels);
      setSteps(turn.steps);
      setSummary(undefined);
      setMessages((prev) => [
        ...prev,
        { id: turnId, who: "you", text, status: "done" },
        { id: replyId, who: "cadre", text: "", status: "pending" },
      ]);

      const controller = new AbortController();
      abortRef.current = controller;

      const publish = () => {
        setSummary(turn.turnSummary);
        patch(replyId, {
          text: turn.replyText,
          status: turn.replyStatus,
          outcome: turn.replyOutcome,
          traceUrl: turn.replyTraceUrl,
          summary: turn.turnSummary,
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
          if (message.event === "trace") {
            turn = applyTrace(turn, JSON.parse(message.data) as TraceEvent);
            publish();
          } else if (message.event === "state") {
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
    //
    // `stepModels` is load-bearing here, not decoration (issue #97). It is a
    // free variable in the body (`freshTurn(stepModels)`), and at mount
    // `App`'s config is still FALLBACK, which carries no `step_models`.
    // `/config` resolving re-renders `App` but changes none of the other
    // three dependencies, so without this entry the first `send` closure of
    // a session keeps `stepModels === undefined` and paints all six chips
    // with no model label — the labels only appeared from the second turn
    // on, once `messages` had changed and incidentally rebuilt the closure.
    // `reset` already takes the same dependency.
    //
    // Note this deliberately does NOT re-seed the idle `steps` state: the
    // pre-turn chips are meant to carry no model label, and
    // `pipeline-idle.spec.ts` asserts exactly that.
    [busy, messages, patch, stepModels],
  );

  return { messages, steps, summary, busy, send, stop, reset };
}
