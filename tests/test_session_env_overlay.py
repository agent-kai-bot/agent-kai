from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import tools
from agent.runtime_utils import session_env_context


class SessionEnvOverlayTests(unittest.TestCase):
    def test_session_overlay_reaches_shell_codex_and_claude_exec(self) -> None:
        calls: list[dict] = []

        def fake_run(_cmd, **kwargs):
            calls.append(kwargs)
            return mock.Mock(stdout="", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            codex_path = Path(temp_dir) / "codex"
            claude_path = Path(temp_dir) / "claude"
            codex_path.write_text("")
            claude_path.write_text("")

            with mock.patch("agent.tools.CODEX_PATH", str(codex_path)), \
                 mock.patch("agent.tools.CLAUDE_PATH", str(claude_path)), \
                 mock.patch("agent.tools.subprocess.run", side_effect=fake_run):
                with session_env_context(
                    {
                        "FORGEJO_TOKEN": "role-pat",
                        "FORGEJO_TOKEN_DEVELOPER": "role-pat",
                        "TASKBOARD_BEARER_TOKEN": "role-bearer",
                    }
                ):
                    tools._shell_exec("true")
                    tools._codex_exec("do work")
                    tools._claude_exec("do work")

        self.assertEqual(len(calls), 3)
        for kwargs in calls:
            self.assertEqual(kwargs["env"]["FORGEJO_TOKEN"], "role-pat")
            self.assertEqual(kwargs["env"]["FORGEJO_TOKEN_DEVELOPER"], "role-pat")
            self.assertEqual(kwargs["env"]["TASKBOARD_BEARER_TOKEN"], "role-bearer")

    def test_without_session_overlay_subprocess_env_uses_process_env(self) -> None:
        calls: list[dict] = []

        def fake_run(_cmd, **kwargs):
            calls.append(kwargs)
            return mock.Mock(stdout="", stderr="", returncode=0)

        with mock.patch("agent.tools.subprocess.run", side_effect=fake_run), \
             mock.patch.dict(
                 "os.environ",
                 {
                     "FORGEJO_TOKEN": "process-pat",
                     "TASKBOARD_BEARER_TOKEN": "process-bearer",
                 },
                 clear=True,
             ):
            tools._shell_exec("true")

        self.assertEqual(calls[0]["env"]["FORGEJO_TOKEN"], "process-pat")
        self.assertEqual(
            calls[0]["env"]["TASKBOARD_BEARER_TOKEN"],
            "process-bearer",
        )
