from pathlib import Path

from agent import tools as agent_tools
from agent.tools import create_docker_sandbox_tool, create_tools


def _tool(toolset, name):
    return next(tool for tool in toolset if tool.name == name)


def test_workspace_bound_docker_sandbox_starts_in_work_and_persists_files(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        source = next(part for part in cmd if part.startswith("--mount=type=bind,"))
        host_path = Path(source.split("source=", 1)[1].split(",target=", 1)[0])
        workdir = next(part for part in cmd if part.startswith("--workdir="))
        (host_path / "created-by-sandbox.txt").write_text("ok\n", encoding="utf-8")

        class Result:
            stdout = workdir.removeprefix("--workdir=") + "\n"
            stderr = ""
            returncode = 0

        return Result()

    monkeypatch.setattr(agent_tools, "_DOCKER_PATH", "/usr/bin/docker")
    monkeypatch.setattr(agent_tools.subprocess, "run", fake_run)

    sandbox = create_docker_sandbox_tool(workspace_host_path=str(tmp_path))
    output = sandbox.invoke({"command": "pwd && touch created-by-sandbox.txt"})

    assert output == "/work"
    assert (tmp_path / "created-by-sandbox.txt").read_text(encoding="utf-8") == "ok\n"
    assert any(part == "--workdir=/work" for part in captured["cmd"])
    assert any(
        part.startswith("--mount=type=bind,") and "target=/work" in part
        for part in captured["cmd"]
    )


def test_human_session_docker_sandbox_has_no_workspace_mount(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd

        class Result:
            stdout = "/tmp\n"
            stderr = ""
            returncode = 0

        return Result()

    monkeypatch.setattr(agent_tools, "_DOCKER_PATH", "/usr/bin/docker")
    monkeypatch.setattr(agent_tools.subprocess, "run", fake_run)

    sandbox = _tool(create_tools(), "docker_sandbox")
    output = sandbox.invoke({"command": "pwd"})

    assert output == "/tmp"
    assert any(part == "--workdir=/tmp" for part in captured["cmd"])
    assert not any(part.startswith("--mount=type=bind,") for part in captured["cmd"])


def test_worker_session_docker_sandbox_uses_primary_repo_path(tmp_path, monkeypatch):
    captured = {}
    primary_repo = tmp_path / "task" / "developer" / "repos" / "repo"
    primary_repo.mkdir(parents=True)
    task_workspace = tmp_path / "task"

    class Session:
        taskboard_dispatcher = {
            "workspace_path": str(task_workspace),
            "primary_repo_path": str(primary_repo),
        }

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd

        class Result:
            stdout = "/work\n"
            stderr = ""
            returncode = 0

        return Result()

    monkeypatch.setattr(agent_tools, "_DOCKER_PATH", "/usr/bin/docker")
    monkeypatch.setattr(agent_tools.subprocess, "run", fake_run)

    sandbox = _tool(create_tools(session=Session()), "docker_sandbox")
    output = sandbox.invoke({"command": "pwd"})

    assert output == "/work"
    mount = next(part for part in captured["cmd"] if part.startswith("--mount=type=bind,"))
    assert f"source={primary_repo}" in mount
    assert "target=/work" in mount


def test_workspace_bound_docker_sandbox_fails_for_missing_workspace_path(
    tmp_path, monkeypatch
):
    missing = tmp_path / "missing"
    monkeypatch.setattr(agent_tools, "_DOCKER_PATH", "/usr/bin/docker")

    sandbox = create_docker_sandbox_tool(workspace_host_path=str(missing))
    output = sandbox.invoke({"command": "pwd"})

    assert "Error: workspace path" in output
    assert str(missing) in output
    assert "does not exist" in output
