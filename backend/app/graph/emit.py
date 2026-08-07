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
    """Pushes graph progress onto the queue the SSE generator is draining."""

    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

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
