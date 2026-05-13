"""Tests for taskboard auto-fire prompt rendering."""

from __future__ import annotations

import unittest

from agent.prompt_renderer import (
    _extract_substitutions,
    _normalize_role,
    render_taskboard_fire_prompt,
)


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

    def test_normalize_role_handles_proper_case_with_space(self) -> None:
        """Proper-case role names with spaces normalize to template stems."""

        cases = {
            "Code Reviewer": "code-reviewer",
            "qa agent": "qa-agent",
            "security auditor": "security-auditor",
            "QA Agent": "qa-agent",
            "Security Auditor": "security-auditor",
        }

        for role, expected in cases.items():
            with self.subTest(role=role):
                self.assertEqual(_normalize_role(role), expected)

    def test_normalize_role_handles_underscores(self) -> None:
        """Underscored role names normalize to hyphenated template stems."""

        cases = {
            "code_reviewer": "code-reviewer",
            "security_auditor": "security-auditor",
        }

        for role, expected in cases.items():
            with self.subTest(role=role):
                self.assertEqual(_normalize_role(role), expected)

    def test_normalize_role_idempotent_on_already_kebab(self) -> None:
        """Already-normalized role names are unchanged."""

        self.assertEqual(_normalize_role("code-reviewer"), "code-reviewer")

    def test_normalize_role_handles_mixed_separators(self) -> None:
        """Mixed separators are collapsed to a single hyphen."""

        self.assertEqual(_normalize_role("Code_Reviewer "), "code-reviewer")

    def test_render_developer_template_substitutes_task_fields(self) -> None:
        """Developer template renders expected task substitutions."""

        rendered = render_taskboard_fire_prompt(
            "developer",
            self._sample_task(),
            worktree_path="/tmp/kai/sessions/taskboard-10153-3-developer",
            primary_repo_path="/tmp/kai/taskboard-repos/agent-kai",
            workspace_manifest_path="/tmp/kai/sessions/taskboard-10153-3-developer/.kai/workspace-manifest.json",
            repo_routing_mode="explicit",
        )

        self.assertIn("# Developer Taskboard Auto-Fire Prompt", rendered)
        self.assertIn("KAI prompt templates for taskboard auto-fire", rendered)
        self.assertIn("Add per-role templates and a safe renderer.", rendered)
        self.assertIn("10021 Taskboard auto-fire", rendered)
        self.assertIn("agent-kai Agent KAI", rendered)
        self.assertIn("task-10153-kai-prompt-templates-for-taskboard-auto-fire", rendered)
        self.assertIn("developer/claude/artifacts/10153-final.txt", rendered)
        self.assertIn("Target repo URL: https://github.com/agent-kai-bot/agent-kai", rendered)
        self.assertIn("Repo routing mode: explicit", rendered)
        self.assertIn("Primary repo path: /tmp/kai/taskboard-repos/agent-kai", rendered)
        self.assertIn("Worktree path: /tmp/kai/sessions/taskboard-10153-3-developer", rendered)
        self.assertIn(
            "Workspace manifest path: /tmp/kai/sessions/taskboard-10153-3-developer/.kai/workspace-manifest.json",
            rendered,
        )
        self.assertIn("Move the task to Code Review only after", rendered)
        self.assertIn("/move` API accepts SPEC v23 canonical statuses", rendered)
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
        self.assertIn("- Target repo URL: ", rendered)
        self.assertIn("- Primary repo path: ", rendered)
        self.assertIn("- Worktree path: ", rendered)
        self.assertIn("- Workspace manifest path: ", rendered)
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

    def test_review_role_prompts_require_terminal_verdict_submit(self) -> None:
        """CR, SA, and QA prompts require the structured verdict tool."""

        role_to_review_type = {
            "code-reviewer": "code",
            "security-auditor": "security",
            "qa-agent": "qa",
        }

        for role, review_type in role_to_review_type.items():
            with self.subTest(role=role):
                rendered = render_taskboard_fire_prompt(role, self._sample_task())
                self.assertIn("taskboard_submit_review_verdict", rendered)
                self.assertIn(f'review_type="{review_type}"', rendered)
                self.assertIn("mandatory terminal action", rendered)
                self.assertIn("A comment is not a verdict", rendered)
                self.assertIn("[AUTO_STATE: done]", rendered)

    def test_render_taskboard_fire_prompt_proper_case_lands_role_template(self) -> None:
        """Proper-case Code Reviewer renders the role-specific prompt."""

        rendered = render_taskboard_fire_prompt("Code Reviewer", self._sample_task())
        expected = render_taskboard_fire_prompt("code-reviewer", self._sample_task())

        self.assertEqual(len(rendered), len(expected))
        self.assertIn("taskboard_submit_review_verdict", rendered)
        self.assertIn('review_type="code"', rendered)

    def test_render_taskboard_fire_prompt_proper_case_review_roles(self) -> None:
        """Proper-case SA and QA roles render their role-specific prompts."""

        role_to_review_type = {
            "Security Auditor": ("security-auditor", "security"),
            "QA Agent": ("qa-agent", "qa"),
        }

        for role, (normalized_role, review_type) in role_to_review_type.items():
            with self.subTest(role=role):
                rendered = render_taskboard_fire_prompt(role, self._sample_task())
                expected = render_taskboard_fire_prompt(
                    normalized_role,
                    self._sample_task(),
                )

                self.assertEqual(len(rendered), len(expected))
                self.assertIn("taskboard_submit_review_verdict", rendered)
                self.assertIn(f'review_type="{review_type}"', rendered)

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
