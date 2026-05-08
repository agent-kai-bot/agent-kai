"""JSONL audit writer for signal-router action decisions."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from daemon.signal_router.actions.base import SafeFormatDict


def default_audit_path_template() -> str:
    """Return the default signal-router audit path template."""

    home = os.getenv("AGENTKAI_HOME", str(Path.home() / ".agentkai"))
    return f"{home}/audit/router_{{date}}.jsonl"


class RouterAuditWriter:
    """Append structured signal-router decisions to JSONL files."""

    def __init__(self, path_template: str | None = None) -> None:
        self.path_template = path_template or default_audit_path_template()

    def __call__(self, decision: dict[str, Any]) -> None:
        self.write(decision)

    def write(self, decision: dict[str, Any], path_template: str | None = None) -> Path:
        row = dict(decision)
        row.setdefault("ts", _utc_now_iso())
        path = self.resolve_path(path_template or row.pop("audit_path_template", None))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str))
            handle.write("\n")
        return path

    def resolve_path(self, path_template: str | None = None) -> Path:
        template = path_template or self.path_template
        now = datetime.now(timezone.utc)
        values = {
            "date": now.strftime("%Y-%m-%d"),
            "hour": now.strftime("%Y-%m-%dT%H"),
        }
        raw = template.format_map(SafeFormatDict(values))
        if "${AGENTKAI_HOME}" in raw and "AGENTKAI_HOME" not in os.environ:
            raw = raw.replace("${AGENTKAI_HOME}", str(Path.home() / ".agentkai"))
        return Path(os.path.expandvars(raw)).expanduser()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
