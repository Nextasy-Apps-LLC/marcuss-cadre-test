interface Props {
  prompts: string[];
  busy: boolean;
  onPick: (prompt: string) => void;
}

/**
 * Starter chips. On a single-purpose bot these carry real weight: they are how
 * a visitor learns what is in scope without having to guess and get refused.
 */
export function Suggestions({ prompts, busy, onPick }: Props) {
  if (prompts.length === 0) return null;

  return (
    <div className="suggestions" data-testid="suggestions">
      {prompts.map((prompt) => (
        <button
          key={prompt}
          type="button"
          disabled={busy}
          aria-disabled={busy}
          onClick={() => onPick(prompt)}
          data-testid="suggested-prompt"
          data-prompt={prompt}
        >
          {prompt}
        </button>
      ))}
    </div>
  );
}
