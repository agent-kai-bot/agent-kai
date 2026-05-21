"""Bounded cancellation helpers for daemon-owned asyncio tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

DEFAULT_TASK_SHUTDOWN_TIMEOUT_SECONDS = 10.0


def _consume_gather_result(task: asyncio.Future[Any]) -> None:
    """Consume late task results so timeout paths do not leak warnings."""

    with suppress(asyncio.CancelledError, Exception):
        task.result()


async def cancel_and_await_tasks(
    tasks: Iterable[asyncio.Task[Any] | None],
    *,
    label: str,
    logger: logging.Logger,
    timeout_seconds: float = DEFAULT_TASK_SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    """Cancel tasks and wait a bounded amount of time for cancellation cleanup."""

    live_tasks = [task for task in tasks if task is not None and not task.done()]
    if not live_tasks:
        return

    for task in live_tasks:
        task.cancel()

    gather = asyncio.gather(*live_tasks, return_exceptions=True)
    try:
        await asyncio.wait_for(asyncio.shield(gather), timeout=timeout_seconds)
    except TimeoutError:
        pending = [task.get_name() for task in live_tasks if not task.done()]
        logger.warning(
            "shutdown timed out waiting %.1fs for %s task(s): %s",
            timeout_seconds,
            label,
            ", ".join(pending) or "unknown",
        )
        gather.cancel()
        gather.add_done_callback(_consume_gather_result)
    except asyncio.CancelledError:
        gather.cancel()
        with suppress(asyncio.CancelledError):
            await gather
        raise
