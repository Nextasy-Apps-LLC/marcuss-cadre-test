import { useEffect, useState } from "react";

import { Composer } from "./components/Composer";
import { Suggestions } from "./components/Suggestions";
import { TracePanel, TraceSummary } from "./components/TracePanel";
import { Transcript } from "./components/Transcript";
import { useCadreChat } from "./lib/useCadreChat";

interface PageConfig {
  greeting: string;
  suggestions: string[];
}

/**
 * Rendered until /config answers. The greeting and chips live server-side so
 * they cannot drift from the topic scope the rail-3 judge classifies against —
 * a chip that gets refused is the worst possible first impression.
 */
const FALLBACK: PageConfig = {
  greeting: "Ask me a question to get started.",
  suggestions: [],
};

export default function App() {
  const [config, setConfig] = useState<PageConfig>(FALLBACK);
  const [traceOpen, setTraceOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;

    fetch("/config")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((data: PageConfig) => {
        if (!cancelled) setConfig(data);
      })
      .catch(() => {
        // Non-fatal: the chat still works without chips. Leave the fallback.
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const chat = useCadreChat(config.greeting);

  return (
    <main className="shell">
      <header className="masthead">
        <h1>cadre</h1>
        <p className="tagline">
          Every guardrail, visible as it runs.
        </p>
      </header>

      <div className={`layout${traceOpen ? " layout--trace-open" : ""}`}>
        <section className="terminal">
          <div className="titlebar">
            <span className="dots" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            <span className="titlebar-path">cadre@marcuss.pro — zsh</span>
            <span className="status">
              <span className={`live${chat.busy ? " live--busy" : ""}`} aria-hidden="true" />
              {chat.busy ? "thinking" : "online"}
            </span>
          </div>

          <Transcript messages={chat.messages} />

          <Suggestions
            prompts={config.suggestions}
            busy={chat.busy}
            onPick={(prompt) => void chat.send(prompt)}
          />

          <TraceSummary
            rails={chat.rails}
            totalMs={chat.totalMs}
            open={traceOpen}
            onToggle={() => setTraceOpen((v) => !v)}
          />

          <Composer
            busy={chat.busy}
            onSend={(text) => void chat.send(text)}
            onStop={chat.stop}
          />
        </section>

        <TracePanel rails={chat.rails} totalMs={chat.totalMs} open={traceOpen} />
      </div>

      <footer className="foot">
        <button type="button" className="linkish" onClick={chat.reset}>
          reset conversation
        </button>
      </footer>
    </main>
  );
}
