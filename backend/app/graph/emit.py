"""How a node tells the client what it is doing.

Nodes take `(state, emit)` and never touch the transport: `emit` is the only
way out of the graph, so the SSE stream is a projection of the graph's
progress rather than a second implementation of it. `/ask` passes a
`QueueEmitter` whose queue the response generator drains; tests can pass any
object with the same two calls.
"""

from __future__ import annotations

import asyncio


class QueueEmitter:
    """Pushes graph progress onto the queue the SSE generator is draining.

    It also carries the turn's `trace_id`. That is not decoration: a node that
    wants to write its own Langfuse span (`retrieve` does — the query it
    searched for and what came back) has to know *which* trace, and the node
    signature is `(state, emit)` by design. The id belongs with the other
    per-request object rather than on `ConversationState`, which is the
    checkpointable channel and no place for anything bound to one visitor
    (KB-008). `None` when tracing is down, and every consumer treats that as
    "write nothing".
    """

    def __init__(self, queue: asyncio.Queue, trace_id: str | None = None) -> None:
        self._queue = queue
        self.trace_id = trace_id

    async def __call__(
        self,
        step: str,
        status: str,
        detail: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        await self._queue.put(
            (
                "state",
                {
                    "step": step,
                    "status": status,
                    "detail": detail,
                    "elapsed_ms": elapsed_ms,
                },
            )
        )

    async def token(self, text: str) -> None:
        await self._queue.put(("token", text))
