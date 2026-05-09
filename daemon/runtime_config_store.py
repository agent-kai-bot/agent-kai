"""Runtime configuration overrides for daemon-owned toggles."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from config import CONFIG_PATH, WORKSPACES_DIR

DEFAULT_RUNTIME_OVERRIDES_PATH = Path(WORKSPACES_DIR) / "runtime_overrides.json"
SIGNAL_ROUTER_PATH = ("daemon", "signal_router")


class _ReadWriteLock:
    """Small multi-reader/single-writer lock for config file access."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False

    def acquire_read(self) -> None:
        with self._condition:
            while self._writer:
                self._condition.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._condition:
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    def acquire_write(self) -> None:
        with self._condition:
            while self._writer or self._readers:
                self._condition.wait()
            self._writer = True

    def release_write(self) -> None:
        with self._condition:
            self._writer = False
            self._condition.notify_all()


class _ReadGuard:
    def __init__(self, lock: _ReadWriteLock) -> None:
        self._lock = lock

    def __enter__(self) -> None:
        self._lock.acquire_read()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._lock.release_read()


class _WriteGuard:
    def __init__(self, lock: _ReadWriteLock) -> None:
        self._lock = lock

    def __enter__(self) -> None:
        self._lock.acquire_write()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._lock.release_write()


class RuntimeConfigStore:
    """JSON-backed runtime override store layered on top of agent-config.json."""

    def __init__(
        self,
        *,
        base_config_path: str | Path | None = None,
        overrides_path: str | Path | None = None,
    ) -> None:
        self.base_config_path = Path(base_config_path or CONFIG_PATH)
        self.overrides_path = Path(overrides_path or DEFAULT_RUNTIME_OVERRIDES_PATH)
        self._lock = _ReadWriteLock()

    def load_base_config(self) -> dict[str, Any]:
        """Read the immutable base daemon config from disk."""

        with _ReadGuard(self._lock):
            return _read_json_object(self.base_config_path, missing_ok=False)

    def load_overrides(self) -> dict[str, Any]:
        """Read runtime overrides, returning an empty overlay when absent."""

        with _ReadGuard(self._lock):
            return _read_json_object(self.overrides_path, missing_ok=True)

    def effective_config(self) -> dict[str, Any]:
        """Return base config recursively overlaid with runtime overrides."""

        with _ReadGuard(self._lock):
            base = _read_json_object(self.base_config_path, missing_ok=False)
            overlay = _read_json_object(self.overrides_path, missing_ok=True)
        return _deep_merge(base, overlay)

    def get_section(self, path: Sequence[str]) -> dict[str, Any]:
        """Return one effective config section addressed by key path."""

        section: Any = self.effective_config()
        for key in path:
            if not isinstance(section, Mapping):
                return {}
            section = section.get(key, {})
        return dict(section) if isinstance(section, Mapping) else {}

    def get_auto_loop_brain(self) -> dict[str, Any]:
        """Return the effective daemon.auto_loop_brain block."""

        return self.get_section(("daemon", "auto_loop_brain"))

    def update_auto_loop_brain_enabled(self, enabled: bool) -> dict[str, Any]:
        """Persist an enabled override and return the new effective block."""

        self.update_section(("daemon", "auto_loop_brain"), {"enabled": bool(enabled)})
        return self.get_auto_loop_brain()

    def get_signal_router(self) -> dict[str, Any]:
        """Return the effective daemon.signal_router block."""

        return self.get_section(SIGNAL_ROUTER_PATH)

    def get_signal_router_live_trades_enabled(self) -> bool:
        """Return whether direct trade actions are allowed to execute live."""

        return bool(self.get_signal_router().get("live_trades_enabled", False))

    def update_signal_router_live_trades_enabled(self, enabled: bool) -> dict[str, Any]:
        """Persist the global signal-router live-trades override."""

        self.update_section(SIGNAL_ROUTER_PATH, {"live_trades_enabled": bool(enabled)})
        return self.get_signal_router()

    def get_signal_router_route_enabled(
        self,
        route_name: str,
        *,
        default: bool = True,
    ) -> bool:
        """Return a per-route enabled override, falling back to route config."""

        overrides = self.load_overrides()
        router = _nested_mapping(overrides, SIGNAL_ROUTER_PATH)
        routes = router.get("routes") if isinstance(router, Mapping) else None
        if not isinstance(routes, Mapping):
            return bool(default)
        route_overlay = routes.get(route_name)
        if not isinstance(route_overlay, Mapping):
            return bool(default)
        enabled = route_overlay.get("enabled")
        return bool(enabled) if isinstance(enabled, bool) else bool(default)

    def update_signal_router_route_enabled(
        self,
        route_name: str,
        enabled: bool,
    ) -> dict[str, Any]:
        """Persist one per-route enabled override."""

        self.update_section(
            SIGNAL_ROUTER_PATH,
            {"routes": {str(route_name): {"enabled": bool(enabled)}}},
        )
        return self.get_signal_router()

    def update_section(self, path: Sequence[str], patch: Mapping[str, Any]) -> None:
        """Merge a patch into one override section and atomically persist it."""

        if not path:
            raise ValueError("override path cannot be empty")
        with _WriteGuard(self._lock):
            overrides = _read_json_object(self.overrides_path, missing_ok=True)
            cursor = overrides
            for key in path[:-1]:
                next_value = cursor.get(key)
                if not isinstance(next_value, dict):
                    next_value = {}
                    cursor[key] = next_value
                cursor = next_value
            leaf_key = path[-1]
            existing = cursor.get(leaf_key)
            if not isinstance(existing, dict):
                existing = {}
            cursor[leaf_key] = _deep_merge(existing, dict(patch))
            _write_json_object_atomic(self.overrides_path, overrides)


def _read_json_object(path: Path, *, missing_ok: bool) -> dict[str, Any]:
    if not path.exists():
        if missing_ok:
            return {}
        raise FileNotFoundError(path)
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return dict(payload)


def _write_json_object_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            fd = -1
            json.dump(payload, tmp_file, indent=2, sort_keys=True)
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, path)
    finally:
        if fd != -1:
            os.close(fd)
        if tmp_path:
            with suppress(FileNotFoundError):
                Path(tmp_path).unlink()


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        elif isinstance(current, list) and isinstance(value, Mapping):
            merged[key] = _merge_named_list(current, value)
        else:
            merged[key] = value
    return merged


def _nested_mapping(payload: Mapping[str, Any], path: Sequence[str]) -> Mapping[str, Any]:
    cursor: Any = payload
    for key in path:
        if not isinstance(cursor, Mapping):
            return {}
        cursor = cursor.get(key, {})
    return cursor if isinstance(cursor, Mapping) else {}


def _merge_named_list(
    base: Sequence[Any],
    overlay_by_name: Mapping[str, Any],
) -> list[Any]:
    """Merge a {"name": patch} overlay into a list of named config objects."""

    merged: list[Any] = []
    seen: set[str] = set()
    for item in base:
        if not isinstance(item, Mapping):
            merged.append(item)
            continue
        name = item.get("name")
        if not isinstance(name, str):
            merged.append(dict(item))
            continue
        seen.add(name)
        patch = overlay_by_name.get(name)
        if isinstance(patch, Mapping):
            merged.append(_deep_merge(item, patch))
        else:
            merged.append(dict(item))
    for name, patch in overlay_by_name.items():
        if name in seen or not isinstance(patch, Mapping):
            continue
        merged.append({"name": name, **dict(patch)})
    return merged
