from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "start-kai-daemon.sh"
REQUIRED_ENV = (
    "TASKBOARD_URL",
    "TASKBOARD_BEARER_TOKEN",
    "KAI_TASKBOARD_WEBHOOK_SECRET",
    "TASKBOARD_AGENT_TOKEN_DEVELOPER",
    "TASKBOARD_AGENT_TOKEN_CODE_REVIEWER",
    "TASKBOARD_AGENT_TOKEN_SECURITY_AUDITOR",
    "TASKBOARD_AGENT_TOKEN_QA_AGENT",
    "TASKBOARD_AGENT_TOKEN_ARCHITECT",
    "TASKBOARD_AGENT_TOKEN_ORCHESTRATOR",
    "KAI_TRUSTED_AUTONOMOUS",
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_start_script_has_valid_bash_syntax() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_force_missing_env_does_not_kill_existing_daemon(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = tmp_path / "calls.log"

    _write_executable(fake_bin / "pgrep", "#!/usr/bin/env bash\necho 4242\n")
    _write_executable(
        fake_bin / "kill",
        "#!/usr/bin/env bash\necho \"kill $*\" >>\"$CALLS_FILE\"\n",
    )
    _write_executable(
        fake_bin / "pkill",
        "#!/usr/bin/env bash\necho \"pkill $*\" >>\"$CALLS_FILE\"\n",
    )

    env = os.environ.copy()
    for key in REQUIRED_ENV:
        env.pop(key, None)
    env.update(
        {
            "CALLS_FILE": str(calls),
            "KAI_DAEMON_REPO": str(repo),
            "KAI_DAEMON_LOG": str(tmp_path / "kai-daemon.log"),
            "KAI_DAEMON_PIDFILE": str(tmp_path / "kai-daemon.pid"),
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "--force"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 2
    assert "Missing required env:" in result.stderr
    assert not calls.exists()
