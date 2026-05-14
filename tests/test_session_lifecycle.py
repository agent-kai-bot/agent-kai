"""Session lifecycle coverage for scheduled jobs, idle TTL, and disk GC."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from daemon.core import (
    get_indexed_session,
    list_indexed_sessions,
    upsert_indexed_session,
)
from daemon.scheduler import Scheduler
from daemon.server import (
    DaemonServer,
    SessionLifecycleConfig,
    _utc_now,
)


class _FakeRunner:
    def __init__(self) -> None:
        self.chat_history = []

    async def run(self, user_input: str, **_kwargs):
        yield {"type": "final", "data": f"done:{user_input}"}

    def set_auto_mode(self, enabled: bool, max_iterations: int = 40):
        del enabled, max_iterations

    def consume_auto_pause_reason(self):
        return None


def _fake_attach_runtime(
    session,
    *,
    bus=None,
    agent_name="kai",
    signal_consumer=None,
    scheduler=None,
):
    del bus, signal_consumer, scheduler
    runner = _FakeRunner()
    session.agent_runner = runner
    session.agent_name = agent_name
    runner.chat_history = session.chat_history
    return runner


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name) / "sessions"
        self._root_patch = mock.patch("daemon.core.SESSIONS_ROOT_DIR", self.base_dir)
        self._index_patch = mock.patch(
            "daemon.core.SESSION_INDEX_PATH",
            self.base_dir / "index.json",
        )
        self._root_patch.start()
        self._index_patch.start()

    def tearDown(self) -> None:
        self._index_patch.stop()
        self._root_patch.stop()
        self._tmp.cleanup()

    def _server(self, *, ttl: float = 86400.0, retention: float = 604800.0) -> DaemonServer:
        return DaemonServer(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=None,
            session_lifecycle_config=SessionLifecycleConfig(
                idle_ttl_seconds=ttl,
                sweep_interval_seconds=300.0,
                on_disk_retention_seconds=retention,
            ),
        )

    def _scheduler(self, server: DaemonServer) -> Scheduler:
        scheduler = Scheduler(
            dispatch_callback=server._handle_scheduled_job_trigger,
            event_bus=server.event_bus,
            event_callback=server._handle_scheduler_event,
            jobs_path=self.base_dir / "scheduler" / "jobs.json",
        )
        server.scheduler = scheduler
        return scheduler

    def _schedule_absolute_job(
        self,
        scheduler: Scheduler,
        *,
        job_id: str,
        owner_session: str,
        prompt: str = "Check BTC",
    ):
        scheduler.schedule_job(
            {
                "id": job_id,
                "type": "absolute",
                "spec": {"at": "2026-04-10T00:01:00+00:00"},
                "prompt": prompt,
                "owner_session": owner_session,
                "created_at": "2026-04-10T00:00:00+00:00",
                "created_by": "agent",
            },
            persist=False,
        )
        return scheduler.get_job(job_id)

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_scheduled_job_fire_closes_cron_wake_session_on_success(
        self,
        attach_runtime,
    ):
        attach_runtime.side_effect = _fake_attach_runtime
        server = self._server()
        scheduler = self._scheduler(server)
        session_name = "agent:kai:cron-wake:run_success"
        managed = await server.get_or_create_session(session_name, create_if_missing=True)
        managed.session.save()
        events = managed.session.subscribe_events()
        job = self._schedule_absolute_job(
            scheduler,
            job_id="job-success",
            owner_session=session_name,
        )

        await server._handle_scheduled_job_trigger(job, _utc_now())

        self.assertNotIn(session_name, server.sessions)
        self.assertIsNone(get_indexed_session(session_name))
        self.assertFalse((self.base_dir / f"{session_name}.json").exists())
        updated = scheduler.get_job("job-success")
        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.last_result_preview, "done:Check BTC")
        topics = [events.get_nowait().topic for _ in range(events.qsize())]
        self.assertIn("scheduled_job.session_closed", topics)

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_scheduled_job_fire_closes_cron_wake_session_on_failure(
        self,
        attach_runtime,
    ):
        attach_runtime.side_effect = _fake_attach_runtime
        server = self._server()
        scheduler = self._scheduler(server)
        session_name = "agent:kai:cron-wake:run_failure"
        managed = await server.get_or_create_session(session_name, create_if_missing=True)
        managed.session.save()
        job = self._schedule_absolute_job(
            scheduler,
            job_id="job-failure",
            owner_session=session_name,
        )

        async def raise_from_run_input(*_args, **_kwargs):
            raise RuntimeError("prompt failed")

        server.run_input = raise_from_run_input  # type: ignore[method-assign]

        await server._handle_scheduled_job_trigger(job, _utc_now())

        self.assertNotIn(session_name, server.sessions)
        self.assertIsNone(get_indexed_session(session_name))
        updated = scheduler.get_job("job-failure")
        self.assertEqual(updated.status, "failed")
        self.assertEqual(updated.last_result_preview, "prompt failed")

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_scheduled_job_session_close_is_idempotent(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime
        server = self._server()
        session_name = "agent:kai:cron-wake:run_idempotent"
        managed = await server.get_or_create_session(session_name, create_if_missing=True)
        managed.session.save()

        first = await server._close_scheduled_job_session(
            session_name,
            job_id="job-idempotent",
        )
        second = await server._close_scheduled_job_session(
            session_name,
            job_id="job-idempotent",
        )

        self.assertTrue(first["closed"])
        self.assertFalse(second["closed"])
        self.assertIsNone(get_indexed_session(session_name))

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_repeated_cron_wake_fires_do_not_accumulate_sessions(
        self,
        attach_runtime,
    ):
        attach_runtime.side_effect = _fake_attach_runtime
        server = self._server()
        scheduler = self._scheduler(server)
        baseline = await server.get_or_create_session("operator", create_if_missing=True)
        baseline.session.save()

        for index in range(5):
            session_name = f"agent:kai:cron-wake:run_{index}"
            managed = await server.get_or_create_session(
                session_name,
                create_if_missing=True,
            )
            managed.session.save()
            job = self._schedule_absolute_job(
                scheduler,
                job_id=f"job-{index}",
                owner_session=session_name,
                prompt=f"Check BTC {index}",
            )
            await server._handle_scheduled_job_trigger(job, _utc_now())

        self.assertEqual([entry.name for entry in list_indexed_sessions()], ["operator"])

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_idle_sweeper_closes_sessions_exceeding_ttl(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime
        server = self._server(ttl=60.0)
        managed = await server.get_or_create_session("idle-old", create_if_missing=True)
        managed.session.save()
        old = datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc)
        managed.session.touch_index()

        upsert_indexed_session(
            "idle-old",
            state_path=managed.session.paths.state_path,
            last_activity=old.isoformat().replace("+00:00", "Z"),
        )

        closed = await server.sweep_idle_sessions(
            now=old + timedelta(seconds=61),
        )

        self.assertEqual([item["session"] for item in closed], ["idle-old"])
        self.assertIsNone(get_indexed_session("idle-old"))
        self.assertNotIn("idle-old", server.sessions)

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_idle_sweeper_skips_queued_and_active_ws_sessions(
        self,
        attach_runtime,
    ):
        attach_runtime.side_effect = _fake_attach_runtime
        server = self._server(ttl=60.0)
        now = datetime(2026, 4, 10, 0, 2, tzinfo=timezone.utc)
        old = now - timedelta(seconds=120)

        queued = await server.get_or_create_session("queued", create_if_missing=True)
        queued.session.queue_input("pending")
        queued.session.save()
        upsert_indexed_session(
            "queued",
            state_path=queued.session.paths.state_path,
            last_activity=old.isoformat().replace("+00:00", "Z"),
        )

        connected = await server.get_or_create_session("connected", create_if_missing=True)
        connected.session.save()
        server._register_websocket_client("connected")
        upsert_indexed_session(
            "connected",
            state_path=connected.session.paths.state_path,
            last_activity=old.isoformat().replace("+00:00", "Z"),
        )

        closed = await server.sweep_idle_sessions(now=now)

        self.assertEqual(closed, [])
        self.assertIsNotNone(get_indexed_session("queued"))
        self.assertIsNotNone(get_indexed_session("connected"))
        server._unregister_websocket_client("connected")

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_idle_sweeper_skips_sessions_younger_than_ttl(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime
        server = self._server(ttl=300.0)
        now = datetime(2026, 4, 10, 0, 2, tzinfo=timezone.utc)
        young = now - timedelta(seconds=120)
        managed = await server.get_or_create_session("young", create_if_missing=True)
        managed.session.save()

        upsert_indexed_session(
            "young",
            state_path=managed.session.paths.state_path,
            last_activity=young.isoformat().replace("+00:00", "Z"),
        )

        closed = await server.sweep_idle_sessions(now=now)

        self.assertEqual(closed, [])
        self.assertIsNotNone(get_indexed_session("young"))

    async def test_orphan_disk_file_gc_respects_retention_window(self):
        server = self._server(retention=300.0)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        old_file = self.base_dir / "old-orphan.json"
        old_lock = self.base_dir / "old-orphan.json.lock"
        old_dir = self.base_dir / "old-orphan"
        recent_file = self.base_dir / "recent-orphan.json"
        for path in (old_file, old_lock, recent_file):
            path.write_text("{}", encoding="utf-8")
        old_dir.mkdir()
        now = 1_800_000_000.0
        old_mtime = now - 400.0
        recent_mtime = now - 60.0
        os.utime(old_file, (old_mtime, old_mtime))
        os.utime(old_lock, (old_mtime, old_mtime))
        os.utime(recent_file, (recent_mtime, recent_mtime))

        result = server.gc_orphan_session_files(now=now)

        self.assertIn(str(old_file), result["deleted_files"])
        self.assertIn(str(old_lock), result["deleted_locks"])
        self.assertIn(str(old_dir), result["removed_dirs"])
        self.assertFalse(old_file.exists())
        self.assertFalse(old_lock.exists())
        self.assertFalse(old_dir.exists())
        self.assertTrue(recent_file.exists())

    async def test_orphan_disk_gc_never_touches_index_or_backups(self):
        server = self._server(retention=0.0)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        index = self.base_dir / "index.json"
        backup = self.base_dir / "index.json.bak"
        index_lock = self.base_dir / "index.json.lock"
        for path in (index, backup, index_lock):
            path.write_text("{}", encoding="utf-8")
            os.utime(path, (1.0, 1.0))

        result = server.gc_orphan_session_files(now=1_800_000_000.0)

        self.assertEqual(result["deleted_files"], [])
        self.assertTrue(index.exists())
        self.assertTrue(backup.exists())
        self.assertTrue(index_lock.exists())

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_delete_session_operator_path_still_cleans_up(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime
        server = self._server()
        managed = await server.get_or_create_session("operator-delete", create_if_missing=True)
        managed.session.save()

        result = await server.delete_session("operator-delete")

        self.assertEqual(result, {"deleted": True, "name": "operator-delete"})
        self.assertNotIn("operator-delete", server.sessions)
        self.assertIsNone(get_indexed_session("operator-delete"))
        self.assertFalse((self.base_dir / "operator-delete.json").exists())
        self.assertFalse((self.base_dir / "operator-delete.json.lock").exists())


if __name__ == "__main__":
    unittest.main()
