"""
Conversation workflows — the runtime half (docs/workflow.md Part 5).

A workflow splits a call into stages. One node is active at a time; it owns
the system prompt and the tools reachable while it is active, and each of
its outgoing edges is offered to the LLM as a callable function so the model
advances the conversation by calling one.

  runner.py     the node walk — which node is active, what prompt, what
                tools, what just happened. No voice dependency at all.
  extractor.py  the two background LLM passes over the transcript: variable
                extraction on leaving a node, and context summarization.

The graph model itself is NOT here — it lives in libs/config_sdk/workflow.py,
because Config Service validates the same definition on publish that this
package walks at runtime.
"""

from .extractor import ContextSummarizer, VariableExtractor, summary_threshold_for
from .runner import WorkflowRunner, graph_for

__all__ = [
    "WorkflowRunner", "graph_for",
    "VariableExtractor", "ContextSummarizer", "summary_threshold_for",
]
