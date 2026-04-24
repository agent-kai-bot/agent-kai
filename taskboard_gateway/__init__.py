"""Taskboard compatibility gateway for the local agent runtime.

The package exposes an OpenClaw-compatible API surface so the existing
taskboard can spawn and message local agent runs without taskboard changes.
"""

from taskboard_gateway.app import create_gateway_app

__all__ = ["create_gateway_app"]
