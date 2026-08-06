import { describe, expect, it } from "vitest";

import { parseFrame, readSse } from "./sse";

/** Build a ReadableStream that emits the given strings as separate chunks. */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(chunks: string[]) {
  const out = [];
  for await (const message of readSse(streamOf(chunks))) out.push(message);
  return out;
}

describe("parseFrame", () => {
  it("extracts event name and data", () => {
    expect(parseFrame('event: token\ndata: {"text":"hi"}')).toEqual({
      event: "token",
      data: '{"text":"hi"}',
    });
  });

  it("defaults to `message` when no event line is present", () => {
    expect(parseFrame("data: bare")?.event).toBe("message");
  });

  it("joins multi-line data with newlines", () => {
    expect(parseFrame("event: x\ndata: one\ndata: two")?.data).toBe("one\ntwo");
  });

  it("returns null for a comment-only frame", () => {
    // The `: ping` heartbeat exists to hold the connection open and carries
    // nothing to act on — surfacing it as a message would break consumers.
    expect(parseFrame(": ping")).toBeNull();
  });

  it("returns null when there is no data line", () => {
    expect(parseFrame("event: token")).toBeNull();
  });
});

describe("readSse", () => {
  it("yields each frame in order", async () => {
    const messages = await collect([
      'event: rail\ndata: {"rail_id":"rail1"}\n\n',
      'event: token\ndata: {"text":"a"}\n\n',
    ]);
    expect(messages.map((m) => m.event)).toEqual(["rail", "token"]);
  });

  it("reassembles a frame split across chunk boundaries", async () => {
    // The network decides where chunks break; it has no idea where our frames
    // are. This is the failure that silently truncates a stream if the buffer
    // is drained naively.
    const messages = await collect(['event: tok', 'en\ndata: {"text":"sp', 'lit"}\n\n']);
    expect(messages).toHaveLength(1);
    expect(JSON.parse(messages[0]!.data)).toEqual({ text: "split" });
  });

  it("yields several frames arriving in one chunk", async () => {
    const messages = await collect([
      'event: token\ndata: {"text":"a"}\n\nevent: token\ndata: {"text":"b"}\n\n',
    ]);
    expect(messages).toHaveLength(2);
  });

  it("skips heartbeats without breaking the surrounding stream", async () => {
    const messages = await collect([
      ": ping\n\n",
      'event: done\ndata: {"refused":false}\n\n',
    ]);
    expect(messages).toHaveLength(1);
    expect(messages[0]!.event).toBe("done");
  });

  it("yields a trailing frame that has no blank line after it", async () => {
    const messages = await collect(['event: done\ndata: {"refused":true}']);
    expect(messages).toHaveLength(1);
  });

  it("yields nothing for an empty stream", async () => {
    expect(await collect([])).toEqual([]);
  });

  it("preserves multi-byte characters split across chunks", async () => {
    // "é" is two bytes in UTF-8. A decoder without `stream: true` would emit a
    // replacement character when the split lands between them.
    const encoder = new TextEncoder();
    const bytes = encoder.encode('event: token\ndata: {"text":"café"}\n\n');
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, 31));
        controller.enqueue(bytes.slice(31));
        controller.close();
      },
    });

    const out = [];
    for await (const message of readSse(stream)) out.push(message);
    expect(JSON.parse(out[0]!.data)).toEqual({ text: "café" });
  });
});
