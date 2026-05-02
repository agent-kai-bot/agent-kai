"""Tests for taskboard auto-fire prompt rendering."""

from __future__ import annotations

import unittest

from agent.prompt_renderer import _extract_substitutions, render_taskboard_fire_prompt


class PromptRendererTests(unittest.TestCase):
    """Validate deterministic taskboard prompt rendering."""

    def _sample_task(self) -> dict:
        """Return a representative taskboard task payload."""

        return {
            "id": 10153,
            "title": "KAI prompt templates for taskboard auto-fire",
            "description": "Add per-role templates and a safe renderer.",
            "agentId": "developer",
            "agent": "Developer",
            "taskType": "feature",
            "priority": "high",
            "project": {
                "name": "Agent KAI",
                "slug": "agent-kai",
                "repoUrl": "https://github.com/agent-kai-bot/agent-kai",
                "defaultBranch": "main",
            },
            "epic": {"id": 10021, "title": "Taskboard auto-fire"},
            "sourceRef": "#10153",
            "taskUrl": "https://taskboard.example/tasks/10153",
            "commentsUrl": "https://taskboard.example/tasks/10153/comments",
            "fireGeneration": 3,
            "sessionGeneration": 9,
            "outputTarget": "developer/claude/artifacts/10153-final.txt",
        }

    def test_render_developer_template_substitutes_task_fields(self) -> None:
        """Developer template renders expected task substitutions."""

        rendered = render_taskboard_fire_prompt("developer", self._sample_task())

        self.assertIn("# Developer Taskboard Auto-Fire Prompt", rendered)
        self.assertIn("KAI prompt templates for taskboard auto-fire", rendered)
        self.assertIn("Add per-role templates and a safe renderer.", rendered)
        self.assertIn("10021 Taskboard auto-fire", rendered)
        self.assertIn("agent-kai Agent KAI", rendered)
        self.assertIn("task-10153-kai-prompt-templates-for-taskboard-auto-fire", rendered)
        self.assertIn("developer/claude/artifacts/10153-final.txt", rendered)
        self.assertIn("Worktree path: ", rendered)
        for placeholder in (
            "{title}",
            "{description}",
            "{task_id}",
            "{epic_id}",
            "{epic_title}",
            "{project_slug}",
            "{project_name}",
            "{priority}",
            "{task_type}",
            "{branch_name_suggestion}",
        ):
            self.assertNotIn(placeholder, rendered)

    def test_unknown_role_uses_default_template(self) -> None:
        """Unknown role names fall back to the default template."""

        rendered = render_taskboard_fire_prompt("missing-role", self._sample_task())

        self.assertIn("# Taskboard Auto-Fire Prompt", rendered)
        self.assertNotIn("# Developer Taskboard Auto-Fire Prompt", rendered)
        self.assertIn("KAI prompt templates for taskboard auto-fire", rendered)

    def test_missing_task_fields_render_empty_without_key_error(self) -> None:
        """Missing task fields do not raise and render as empty strings."""

        rendered = render_taskboard_fire_prompt(
            "developer",
            {"id": 123, "title": "Sparse task"},
        )

        self.assertIn("Sparse task", rendered)
        self.assertIn("- Epic: ", rendered)
        self.assertNotIn("{epic_title}", rendered)

    def test_all_role_templates_are_loadable(self) -> None:
        """All taskboard fire templates render successfully."""

        for role in (
            "architect",
            "developer",
            "code-reviewer",
            "security-auditor",
            "qa-agent",
            "default",
        ):
            with self.subTest(role=role):
                rendered = render_taskboard_fire_prompt(role, self._sample_task())
                self.assertIn("STOP: TASKBOARD_FIRE_PROMPT_END", rendered)
                self.assertIn("Task ID: 10153", rendered)

    def test_task_id_is_populated_for_non_empty_task(self) -> None:
        """Any non-empty task payload receives a task_id substitution."""

        substitutions = _extract_substitutions({"title": "Task without id"})

        self.assertEqual(substitutions["task_id"], "unknown")

    def test_branch_name_suggestion_uses_task_id_and_title_slug(self) -> None:
        """Branch suggestions are derived from task id and title slug."""

        substitutions = _extract_substitutions(
            {
                "id": 10153,
                "title": "KAI prompt templates for taskboard auto-fire (Phase 3)",
            }
        )

        self.assertEqual(
            substitutions["branch_name_suggestion"],
            "task-10153-kai-prompt-templates-for-taskboard-auto-fire-phase-3",
        )


if __name__ == "__main__":
    unittest.main()
