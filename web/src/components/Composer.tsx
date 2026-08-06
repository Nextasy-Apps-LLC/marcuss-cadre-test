import { useState, type FormEvent } from "react";

const MAX_LEN = 2000;

interface Props {
  busy: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}

export function Composer({ busy, onSend, onStop }: Props) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  function submit(event: FormEvent) {
    event.preventDefault();
    const text = value.trim();
    if (!text) return;

    // Mirrors the backend's rail-1 cap. Catching it here turns a wasted round
    // trip into an instant inline message.
    if (text.length > MAX_LEN) {
      setError("Message too long — please shorten it.");
      return;
    }

    setError(null);
    setValue("");
    onSend(text);
  }

  return (
    <>
      <form className="composer" onSubmit={submit}>
        <span className="composer-prompt" aria-hidden="true">
          $
        </span>
        <input
          className="composer-input"
          data-testid="chat-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={busy}
          aria-disabled={busy}
          maxLength={MAX_LEN}
          autoComplete="off"
          placeholder="Ask a question…"
          aria-label="Your question"
        />
        {/* Visually hidden: Enter submits, but the form still needs a real
            submit control for assistive tech and mobile keyboards. */}
        <button type="submit" className="visually-hidden" data-testid="chat-send">
          Send
        </button>
        {busy && (
          <button
            type="button"
            className="composer-stop"
            onClick={onStop}
            aria-label="Stop generating"
            data-testid="chat-stop"
          >
            ■
          </button>
        )}
      </form>

      {error && (
        <div className="composer-error" role="alert" data-testid="chat-input-error">
          {error}
        </div>
      )}
    </>
  );
}
