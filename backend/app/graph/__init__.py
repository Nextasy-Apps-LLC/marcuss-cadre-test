"""The LangGraph conversation engine.

`state` is the typed channel every node reads and writes, `models` is the set
of seams the model-backed steps call, `nodes` implements one step per function,
and `build` wires them into the `StateGraph` whose terminals are the outcomes
the client sees.
"""
