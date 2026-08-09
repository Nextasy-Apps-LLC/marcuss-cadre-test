import { useEffect, useRef } from "react";

import { linkify } from "../lib/linkify";
import { formatTurnSummary } from "../lib/usage";
import type { ChatMessage } from "../types";

interface Props {
  messages: ChatMessage[];
}

const WHO_LABEL: Record<ChatMessage["who"], string> = {
  you: "you",
  cadre: "cadreai-bot",
  system: "",
};

export function Transcript({ messages }: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  // Follow the tail as tokens stream in. `messages` changes on every chunk, so
  // this fires often — `block: "end"` keeps it cheap and avoids yanking the
  // whole page around on mobile.
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  return (
    <div className="transcript" data-testid="chat-message-list">
      {messages.map((message) => (
        <div
          key={message.id}
          className={`msg msg--${message.who}`}
          data-status={message.status}
          aria-busy={message.status === "pending" || message.status === "streaming"}
        >
          {WHO_LABEL[message.who] && (
            <span className="msg-who">{WHO_LABEL[message.who]}</span>
          )}
          <span
            className="msg-body"
            // Only the live reply is a polite live region. Marking every
            // message would make a screen reader re-announce the whole
            // transcript on each token.
            aria-live={message.status === "streaming" ? "polite" : undefined}
          >
            {message.who === "cadre" && message.status === "done"
              ? linkify(message.text).map((segment, i) =>
                  segment.type === "link" ? (
                    <a
                      key={i}
                      href={segment.url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {segment.label}
                    </a>
                  ) : (
                    // eslint-disable-next-line react/no-array-index-key -- segments are a stable, order-only split of static settled text
                    <span key={i}>{segment.value}</span>
                  ),
                )
              : message.text}
            {message.who === "cadre" && message.traceUrl && (
              <>
                <a
                  href={message.traceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="trace-link"
                  data-testid="trace-link"
                  aria-describedby={`trace-note-${message.id}`}
                >
                  View trace ↗
                </a>
                {/* Langfuse Cloud ingestion is async (KB-020) — a trace
                    clicked immediately after the turn ends routinely 404s.
                    This footnote is the cheap, honest interim mitigation;
                    associated with the link via aria-describedby so it
                    isn't orphaned text for screen readers. */}
                <span
                  id={`trace-note-${message.id}`}
                  className="trace-note"
                  data-testid="trace-note"
                >
                  Traces can take up to 30 seconds to become reachable.
                </span>
              </>
            )}
            {message.status === "pending" && (
              <span className="cursor" aria-hidden="true" />
            )}
          </span>
          {/* The one-line aggregate the `done` event carried (issue #109):
              tokens · cost · latency for this turn. Only on a settled cadre
              reply, and only when the wire sent a summary — tracing was down
              for the turn otherwise, and inventing a zeroed line would read
              as a measurement that never happened (KB-009). */}
          {message.who === "cadre" && message.status === "done" && message.summary && (
            <div className="msg-summary" data-testid="reply-summary">
              {formatTurnSummary(message.summary)}
            </div>
          )}
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}
