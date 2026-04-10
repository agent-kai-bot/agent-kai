"""Tests for the daemon wire-protocol envelopes and validation."""

from __future__ import annotations

import unittest

from daemon.protocol import (
    ChartBarEnvelope,
    NatsEventEnvelope,
    ScheduledJobCreatedEnvelope,
    ScheduledJobTriggeredEnvelope,
    SessionAttachedEnvelope,
    SessionStateSnapshot,
    StatusEnvelope,
    ToolEndEnvelope,
    decode_client_envelope,
    decode_server_envelope,
    encode_envelope,
)


class ClientEnvelopeTests(unittest.TestCase):
    """Validate client-side protocol decoding and field validation."""

    def test_decode_attach_envelope(self):
        envelope = decode_client_envelope(
            {"type": "attach", "session": "terminal", "create_if_missing": True}
        )

        self.assertEqual(envelope.type, "attach")
        self.assertEqual(envelope.session, "terminal")
        self.assertTrue(envelope.create_if_missing)

    def test_reject_extra_fields(self):
        with self.assertRaisesRegex(ValueError, "Extra inputs are not permitted"):
            decode_client_envelope(
                {
                    "type": "attach",
                    "session": "terminal",
                    "create_if_missing": True,
                    "unexpected": 1,
                }
            )

    def test_reject_chart_subscription_without_symbol_and_tf(self):
        with self.assertRaisesRegex(
            ValueError,
            "chart subscriptions require symbol and tf",
        ):
            decode_client_envelope({"type": "subscribe", "channel": "chart"})


class ServerEnvelopeTests(unittest.TestCase):
    """Validate server-side serialization and decoding."""

    def test_session_attached_round_trip(self):
        encoded = encode_envelope(
            SessionAttachedEnvelope(
                type="session_attached",
                session="terminal",
                state=SessionStateSnapshot(
                    chart_symbol="BTC",
                    chat_history=[{"role": "human", "content": "hello"}],
                ),
            )
        )

        decoded = decode_server_envelope(encoded)

        self.assertEqual(decoded.type, "session_attached")
        self.assertEqual(decoded.state.chart_symbol, "BTC")
        self.assertEqual(decoded.state.chat_history[0].role, "human")

    def test_tool_end_omits_null_elapsed_on_encode(self):
        payload = encode_envelope(
            ToolEndEnvelope(type="tool_end", tool="lookup", elapsed_ms=None, ok=True)
        )

        self.assertEqual(payload, {"type": "tool_end", "tool": "lookup", "ok": True})

    def test_chart_bar_and_status_decode(self):
        chart_bar = decode_server_envelope(
            encode_envelope(
                ChartBarEnvelope(
                    type="chart_bar",
                    symbol="BTC-USD",
                    tf="1h",
                    bar={"ts": 1, "c": 2},
                )
            )
        )
        status = decode_server_envelope(
            encode_envelope(
                StatusEnvelope(type="status", activity="thinking...", queue=2)
            )
        )

        self.assertEqual(chart_bar.type, "chart_bar")
        self.assertEqual(chart_bar.symbol, "BTC-USD")
        self.assertEqual(status.type, "status")
        self.assertEqual(status.queue, 2)

    def test_nats_event_round_trip(self):
        decoded = decode_server_envelope(
            encode_envelope(
                NatsEventEnvelope(
                    type="nats_event",
                    direction="pub",
                    subject="agent.broadcast",
                    payload={"message": "hello"},
                )
            )
        )

        self.assertEqual(decoded.type, "nats_event")
        self.assertEqual(decoded.direction, "pub")
        self.assertEqual(decoded.subject, "agent.broadcast")

    def test_scheduled_job_envelopes_round_trip(self):
        created = decode_server_envelope(
            encode_envelope(
                ScheduledJobCreatedEnvelope(
                    type="scheduled_job_created",
                    job={"id": "job-1", "status": "active"},
                )
            )
        )
        triggered = decode_server_envelope(
            encode_envelope(
                ScheduledJobTriggeredEnvelope(
                    type="scheduled_job_triggered",
                    job_id="job-1",
                    fired_at="2026-04-10T00:00:00Z",
                )
            )
        )

        self.assertEqual(created.type, "scheduled_job_created")
        self.assertEqual(created.job["id"], "job-1")
        self.assertEqual(triggered.type, "scheduled_job_triggered")
        self.assertEqual(triggered.job_id, "job-1")


if __name__ == "__main__":
    unittest.main()
