"""Regression tests for the duplicate-user-message bug at LLM-call render time.

Caught 2026-05-06: in scheduled-job BIO turns Dan saw two identical user
messages literally back-to-back before the AI's reply. Root cause was that
``run_input`` appended the user message to ``self.chat_history`` BEFORE
rendering ``_active_llm_chat_history``, and the LangChain ``AgentExecutor``
prompt template re-injected ``{input}`` on top of that history — so the
model received the same HumanMessage twice in a row.

Fix is the ``exclude_trailing_user_input`` knob on
:meth:`AgentRunner._chat_history_for_llm`. These tests pin the contract:

* When invoked normally (no flag), the trailing user message is included.
* When invoked with the flag, the trailing user message is dropped, but
  any prior non-Human trailing message is preserved.
* The dropped message still lives in ``self.chat_history`` (we only render
  a different view; persistence is unchanged).
"""

from __future__ import annotations

import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.core import AgentRunner


class _ChatHistoryHolder:
    """Minimal AgentRunner subclass that bypasses runtime config entirely.

    We only exercise ``_chat_history_for_llm`` here, which reads
    ``self.chat_history`` and a couple of env-driven limits. No LLM, no
    tool registry, no logger — those aren't relevant for the dedup logic.
    """

    def __init__(self, history):
        self.chat_history = list(history)


def _render(history, *, exclude_trailing_user_input=False):
    """Call _chat_history_for_llm via an AgentRunner.__func__ trampoline."""
    holder = _ChatHistoryHolder(history)
    # Bind the unbound function from AgentRunner so we don't have to spin
    # a full runtime up. The method only touches self.chat_history.
    return AgentRunner._chat_history_for_llm(
        holder,
        exclude_trailing_user_input=exclude_trailing_user_input,
    )


class ChatHistoryForLLMDedupTests(unittest.TestCase):
    """Pin the exclude_trailing_user_input contract."""

    def test_default_call_includes_trailing_human_message(self) -> None:
        """Without the flag, the rendered history includes everything."""
        history = [
            HumanMessage(content="first user turn"),
            AIMessage(content="first reply"),
            HumanMessage(content="second user turn"),
        ]
        rendered = _render(history)
        self.assertEqual(len(rendered), 3)
        self.assertIsInstance(rendered[-1], HumanMessage)
        self.assertEqual(rendered[-1].content, "second user turn")

    def test_flag_drops_trailing_human_message(self) -> None:
        """With the flag, the trailing HumanMessage is dropped — the
        AgentExecutor prompt template will re-inject it from {input}."""
        history = [
            HumanMessage(content="first user turn"),
            AIMessage(content="first reply"),
            HumanMessage(content="second user turn — this is the dup risk"),
        ]
        rendered = _render(history, exclude_trailing_user_input=True)
        self.assertEqual(len(rendered), 2)
        self.assertIsInstance(rendered[-1], AIMessage)
        # Nothing in the rendered history matches the would-be-duplicate.
        for message in rendered:
            self.assertNotEqual(
                getattr(message, "content", None),
                "second user turn — this is the dup risk",
            )

    def test_flag_preserves_trailing_non_human_message(self) -> None:
        """If the trailing message isn't a HumanMessage, the flag is a no-op
        (we only drop the *user* message that's about to be re-injected)."""
        history = [
            HumanMessage(content="first user turn"),
            AIMessage(content="first reply"),
            SystemMessage(content="[scheduled job: abc]"),
        ]
        rendered = _render(history, exclude_trailing_user_input=True)
        self.assertEqual(len(rendered), 3)
        self.assertIsInstance(rendered[-1], SystemMessage)

    def test_flag_with_empty_history_is_safe(self) -> None:
        """Empty history + flag is a no-op, not an IndexError."""
        rendered = _render([], exclude_trailing_user_input=True)
        self.assertEqual(rendered, [])

    def test_flag_does_not_mutate_source_history(self) -> None:
        """Persistence is sacred — the call returns a different view but
        does NOT mutate the underlying chat_history list."""
        history = [
            HumanMessage(content="persisted turn"),
        ]
        holder = _ChatHistoryHolder(history)
        AgentRunner._chat_history_for_llm(
            holder, exclude_trailing_user_input=True
        )
        self.assertEqual(len(holder.chat_history), 1)
        self.assertEqual(holder.chat_history[-1].content, "persisted turn")

    def test_full_scheduled_job_pattern_no_dup(self) -> None:
        """End-to-end scheduled-job pattern: system marker → human → render
        for LLM. The rendered history (with flag) ends at the system marker;
        the LangChain executor will inject the human message from {input}."""
        history = [
            HumanMessage(content="prior user turn"),
            AIMessage(content="prior reply"),
            SystemMessage(content="[scheduled job: bio_5m]"),
            HumanMessage(content="Re-analyze BIO from Coinbase every 5 minutes ..."),
        ]
        rendered = _render(history, exclude_trailing_user_input=True)
        self.assertEqual(len(rendered), 3)
        self.assertIsInstance(rendered[-1], SystemMessage)
        # The user message is NOT in the rendered history — it will come
        # from {input} on the executor call, so the model sees it exactly
        # once.
        bio_turns = [
            m for m in rendered
            if isinstance(m, HumanMessage) and "Re-analyze BIO" in m.content
        ]
        self.assertEqual(bio_turns, [])


if __name__ == "__main__":
    unittest.main()
