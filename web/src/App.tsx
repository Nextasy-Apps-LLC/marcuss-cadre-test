import { useEffect, useState } from "react";

import { Composer } from "./components/Composer";
import { PipelineStepper } from "./components/PipelineStepper";
import { Suggestions } from "./components/Suggestions";
import { Transcript } from "./components/Transcript";
import { useCadreChat } from "./lib/useCadreChat";
import type { StepName } from "./types";

interface PageConfig {
  greeting: string;
  suggestions: string[];
  step_models?: Partial<Record<StepName, string>>;
}

/**
 * Rendered until /config answers. The greeting and chips live server-side so
 * they cannot drift from the topic scope the topic_classifier step judges against —
 * a chip that gets refused is the worst possible first impression.
 */
const FALLBACK: PageConfig = {
  greeting: "Ask me a question to get started.",
  suggestions: [],
};

export default function App() {
  const [config, setConfig] = useState<PageConfig>(FALLBACK);
  const [stepperOpen, setStepperOpen] = useState(false);
  const [verbose, setVerbose] = useState(true);

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

  const chat = useCadreChat(config.greeting, config.step_models);

  return (
    <main className="shell">
      <header className="masthead">
        <h1>Cadre</h1>
        <p className="tagline">
          Every guardrail, visible as it runs.
        </p>
      </header>

      <div className={`layout${stepperOpen ? " layout--stepper-open" : ""}`}>
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

          <Composer
            busy={chat.busy}
            onSend={(text) => void chat.send(text)}
            onStop={chat.stop}
          />
        </section>

        <PipelineStepper
          steps={chat.steps}
          summary={chat.summary}
          open={stepperOpen}
          onToggle={() => setStepperOpen((v) => !v)}
          verbose={verbose}
          onVerboseToggle={() => setVerbose((v) => !v)}
        />
      </div>

      <footer className="foot">
        <button type="button" className="linkish" onClick={chat.reset}>
          reset conversation
        </button>
      </footer>
    </main>
  );
}
