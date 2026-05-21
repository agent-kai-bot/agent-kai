"""Tests for Forgejo PR auto-fire prompt rendering."""

from __future__ import annotations

import unittest

from agent.prompt_renderer import (
    _extract_pr_substitutions,
    render_forgejo_pr_fire_prompt,
)


class ForgejoPrRendererTests(unittest.TestCase):
    """Validate deterministic Forgejo PR prompt rendering."""

    def _sample_pr(self) -> dict:
        """Return a representative Forgejo PR payload."""

        return {
            "repo": "Praxis/agent-kai",
            "pr_number": 42,
            "branch": "feature/forgejo-prompts",
            "title": "Add Forgejo PR prompts",
            "body": "Closes taskboard #10174\n\nAdds role-specific prompts.",
            "head_sha": "abc123def4567890",
            "pr_url": "https://forgejo.example/Praxis/agent-kai/pulls/42",
            "taskboard_task_id": 10174,
            "files_changed": [
                {
                    "filename": "agent/prompt_renderer.py",
                    "status": "modified",
                    "additions": 48,
                    "deletions": 6,
                },
                {
                    "filename": "prompts/forgejo-pr-fire/code-reviewer.md.tmpl",
                    "status": "added",
                    "additions": 37,
                    "deletions": 0,
                },
                {
                    "previous_filename": "tests/test_old_renderer.py",
                    "filename": "tests/test_forgejo_pr_renderer.py",
                    "status": "renamed",
                    "changes": 12,
                },
            ],
        }

    def test_code_reviewer_template_substitutes_pr_fields(self) -> None:
        """Code reviewer template renders all required PR substitutions."""

        rendered = render_forgejo_pr_fire_prompt("code-reviewer", self._sample_pr())

        self.assertIn("# Code Reviewer Forgejo PR Auto-Fire Prompt", rendered)
        self.assertIn("Praxis/agent-kai", rendered)
        self.assertIn("Pull request: 42", rendered)
        self.assertIn("feature/forgejo-prompts", rendered)
        self.assertIn("Add Forgejo PR prompts", rendered)
        self.assertIn("Closes taskboard #10174", rendered)
        self.assertIn("abc123def4567890", rendered)
        self.assertIn("3 files changed: 85 additions, 6 deletions", rendered)
        self.assertIn(
            "code-reviewer/claude/artifacts/"
            "forgejo-pr-praxis-agent-kai-42-abc123def456-final.txt",
            rendered,
        )
        for placeholder in (
            "{repo}",
            "{pr_number}",
            "{branch}",
            "{title}",
            "{body}",
            "{files_changed}",
            "{diff_summary}",
            "{head_sha}",
        ):
            self.assertNotIn(placeholder, rendered)

    def test_unknown_role_uses_default_template(self) -> None:
        """Unknown role names fall back to the default Forgejo PR template."""

        rendered = render_forgejo_pr_fire_prompt("missing-role", self._sample_pr())

        self.assertIn("# Forgejo PR Reviewer Prompt", rendered)
        self.assertNotIn("# Code Reviewer Forgejo PR Auto-Fire Prompt", rendered)
        self.assertIn("Add Forgejo PR prompts", rendered)

    def test_missing_pr_fields_render_empty_without_key_error(self) -> None:
        """Missing PR fields do not raise and render as empty strings."""

        rendered = render_forgejo_pr_fire_prompt(
            "code-reviewer",
            {"title": "Sparse PR"},
        )

        self.assertIn("Sparse PR", rendered)
        self.assertIn("- Repository: ", rendered)
        self.assertIn("- Head SHA: ", rendered)
        self.assertNotIn("{repo}", rendered)
        self.assertNotIn("{head_sha}", rendered)

    def test_all_role_templates_are_loadable(self) -> None:
        """All Forgejo PR fire role templates render successfully."""

        for role in ("code-reviewer", "security-auditor", "qa-agent"):
            with self.subTest(role=role):
                rendered = render_forgejo_pr_fire_prompt(role, self._sample_pr())
                self.assertIn("STOP: FORGEJO_PR_FIRE_PROMPT_END", rendered)
                self.assertIn("Repository: Praxis/agent-kai", rendered)

    def test_review_role_prompts_require_formal_forgejo_review_submit(self) -> None:
        """Forgejo PR reviewer prompts must land a formal PR review."""

        expected_roles = {
            "code-reviewer": ("Code Reviewer", "agent-code-reviewer"),
            "security-auditor": ("Security Auditor", "agent-security-auditor"),
            "qa-agent": ("QA Agent", "agent-qa"),
        }

        for role, (display_role, reviewer_user) in expected_roles.items():
            with self.subTest(role=role):
                rendered = render_forgejo_pr_fire_prompt(role, self._sample_pr())

                self.assertIn("## Submit formal review", rendered)
                self.assertIn("pr-review agent-kai 42 <approved|changes>", rendered)
                self.assertIn(f'"{display_role}" --body-file', rendered)
                self.assertIn("fg-reviews agent-kai 42", rendered)
                self.assertIn(f"`{reviewer_user}`", rendered)
                self.assertIn("Report the review id", rendered)
                self.assertIn("hard failure", rendered)

    def test_forgejo_cli_target_is_derived_for_rendered_submit_command(self) -> None:
        """Rendered formal-review commands use the CLI org/repo shape."""

        substitutions = _extract_pr_substitutions(self._sample_pr())

        self.assertEqual(substitutions["forgejo_org"], "Praxis")
        self.assertEqual(substitutions["forgejo_repo"], "agent-kai")

    def test_diff_summary_is_derived_from_files_changed(self) -> None:
        """Diff summary is derived from the files_changed array."""

        substitutions = _extract_pr_substitutions(self._sample_pr())

        self.assertEqual(
            substitutions["diff_summary"],
            "\n".join(
                (
                    "3 files changed: 85 additions, 6 deletions",
                    "- modified agent/prompt_renderer.py (+48/-6)",
                    "- added prompts/forgejo-pr-fire/code-reviewer.md.tmpl (+37/-0)",
                    "- renamed tests/test_old_renderer.py -> "
                    "tests/test_forgejo_pr_renderer.py (12 changes)",
                )
            ),
        )


if __name__ == "__main__":
    unittest.main()
