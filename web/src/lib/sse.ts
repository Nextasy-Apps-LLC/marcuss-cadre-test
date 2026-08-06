/**
 * A minimal SSE reader over `fetch`.
 *
 * `EventSource` is not usable here: it only issues GET requests and cannot set
 * headers, and `/ask` is a POST carrying a JSON body. So we read the response
 * body ourselves and parse the wire format by hand.
 */

export interface SseMessage {
  event: string;
  data: string;
}

/**
 * Split a raw SSE frame into its event name and joined data payload.
 * Returns null for frames with no `data:` line — comment-only frames like the
 * `: ping` heartbeat, which exist purely to keep intermediaries from reaping
 * an idle connection and carry nothing to act on.
 */
export function parseFrame(frame: string): SseMessage | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue; // comment / heartbeat
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    }
  }

  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n") };
}

/**
 * Yield each SSE message as it arrives.
 *
 * Frames are separated by a blank line, and a chunk boundary can land anywhere
 * — including mid-frame — so the buffer is only drained at `\n\n` boundaries
 * and whatever trails is carried into the next read.
 */
export async function* readSse(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<SseMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (frame.trim()) {
          const message = parseFrame(frame);
          if (message) yield message;
        }
      }

      if (signal?.aborted) break;
    }

    // A final frame with no trailing blank line still counts.
    if (buffer.trim()) {
      const message = parseFrame(buffer);
      if (message) yield message;
    }
  } finally {
    reader.releaseLock();
  }
}
