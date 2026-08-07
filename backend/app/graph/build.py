"""Wiring — the pipeline from plan.md's diagram, as a `StateGraph`.

    validate_input ──fail──▶ refuse
          │ pass
    injection_check ──fail──▶ refuse
          │ pass
    topic_classifier ──off_topic──▶ refuse
          │ in_scope         └──needs_human──▶ escalate
    retrieve
          │
    brain
          │
    output_safety ──fail──▶ refuse
          │ pass
    done (answered)

Every terminal is an explicit node, so "what happened to this turn" is always
answerable from the graph rather than reconstructed from the transcript.

`emit` is not part of the state: it is per-request, and putting a live queue in
a checkpointable channel would be a way to leak one visitor's stream into
another's. It rides `config["configurable"]["emit"]` instead, and `_bind`
unpacks it so the nodes keep their plain `(state, emit)` signature.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from app.graph import nodes
from app.graph.state import ConversationState, last_step

Node = Callable[[ConversationState, object], Awaitable[ConversationState]]


def _bind(node: Node):
    async def run(state: ConversationState, config) -> ConversationState:
        return await node(state, config["configurable"]["emit"])

    run.__name__ = node.__name__
    return run


def _after_check(next_step: str):
    """A guard either refuses the turn or hands it to the next step."""

    def route(state: ConversationState) -> str:
        step = last_step(state)
        return "refuse" if step and step["status"] == "fail" else next_step

    return route


def _after_topic(state: ConversationState) -> str:
    step = last_step(state)
    if step and step["status"] == "fail":
        return "refuse"
    if step and step["detail"] == "needs_human":
        return "escalate"
    return nodes.RETRIEVE


def build_graph():
    graph = StateGraph(ConversationState)

    graph.add_node(nodes.VALIDATE_INPUT, _bind(nodes.validate_input))
    graph.add_node(nodes.INJECTION_CHECK, _bind(nodes.injection_check))
    graph.add_node(nodes.TOPIC_CLASSIFIER, _bind(nodes.topic_classifier))
    graph.add_node(nodes.RETRIEVE, _bind(nodes.retrieve))
    graph.add_node(nodes.BRAIN, _bind(nodes.brain))
    graph.add_node(nodes.OUTPUT_SAFETY, _bind(nodes.output_safety))
    graph.add_node("refuse", _bind(nodes.refuse))
    graph.add_node("escalate", _bind(nodes.escalate))

    graph.add_edge(START, nodes.VALIDATE_INPUT)
    graph.add_conditional_edges(
        nodes.VALIDATE_INPUT,
        _after_check(nodes.INJECTION_CHECK),
        {"refuse": "refuse", nodes.INJECTION_CHECK: nodes.INJECTION_CHECK},
    )
    graph.add_conditional_edges(
        nodes.INJECTION_CHECK,
        _after_check(nodes.TOPIC_CLASSIFIER),
        {"refuse": "refuse", nodes.TOPIC_CLASSIFIER: nodes.TOPIC_CLASSIFIER},
    )
    graph.add_conditional_edges(
        nodes.TOPIC_CLASSIFIER,
        _after_topic,
        {
            "refuse": "refuse",
            "escalate": "escalate",
            nodes.RETRIEVE: nodes.RETRIEVE,
        },
    )
    graph.add_edge(nodes.RETRIEVE, nodes.BRAIN)
    graph.add_edge(nodes.BRAIN, nodes.OUTPUT_SAFETY)
    graph.add_conditional_edges(
        nodes.OUTPUT_SAFETY,
        _after_check(END),
        {"refuse": "refuse", END: END},
    )
    graph.add_edge("refuse", END)
    graph.add_edge("escalate", END)

    return graph.compile()
