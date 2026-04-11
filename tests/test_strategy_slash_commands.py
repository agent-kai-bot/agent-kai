"""Tests for ASO optimizer and strategy slash command handling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.strategy_ir import (
    Condition,
    ConditionOperator,
    ConstantValue,
    CostsSpec,
    EntrySpec,
    ExitSpec,
    IndicatorSpec,
    IndicatorType,
    RiskSpec,
    StrategyIR,
    TimeExitSpec,
)
from daemon.server import DaemonServer, ManagedSession
from tui.terminal import TradingTerminal


class _FakeRuntime:
    def __init__(self) -> None:
        self.running = False
        self.paused = False
        self.started_with: list[int] = []
        self.cycles = [
            {
                "status": "accepted",
                "reason": "improved sharpe",
                "completed_at": "2026-04-11T10:00:00+00:00",
            }
        ]

    def optimizer_state(self) -> dict:
        return {
            "running": self.running,
            "paused": self.paused,
            "started_at": None,
            "last_completed_at": "2026-04-11T10:00:00+00:00",
            "last_stop_reason": "completed",
            "last_error": None,
            "requested_cycles": self.started_with[-1] if self.started_with else None,
            "last_cycle_result": self.cycles[-1],
        }

    def start_optimizer(self, store, max_cycles: int) -> dict:
        del store
        self.running = True
        self.started_with.append(max_cycles)
        return {"ok": True, "kind": "optimizer_start", "max_cycles": max_cycles}

    def pause_optimizer(self) -> dict:
        self.running = False
        self.paused = True
        return {"ok": True, "kind": "optimizer_pause", "paused": True}

    def recent_cycle_results(self, limit: int = 5) -> list[dict]:
        return list(self.cycles[-limit:])


def _build_ir(name: str) -> StrategyIR:
    return StrategyIR(
        name=name,
        symbol="BTC-USD",
        timeframe="1h",
        indicators=[IndicatorSpec(type=IndicatorType.SMA, period=5, alias="sma_fast")],
        entry=EntrySpec(
            conditions=[
                Condition(
                    indicator="sma_fast",
                    operator=ConditionOperator.ABOVE,
                    target=ConstantValue(value=100.0),
                )
            ]
        ),
        exit=ExitSpec(time_exit=TimeExitSpec(max_bars=2)),
        risk=RiskSpec(max_position_pct=0.1, max_drawdown_pct=0.2),
        costs=CostsSpec(),
        max_warmup=5,
        warmup_bars=StrategyIR.compute_warmup_bars(5),
    )


class StrategySlashCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root_dir = Path(self.temp_dir.name) / "alpha"
        store_path = root_dir / "strategies" / "aso.db"
        self.session = SimpleNamespace(
            name="alpha",
            is_remote=False,
            paths=SimpleNamespace(root_dir=root_dir),
            strategy_store_path=str(store_path),
            strategy_runtime=_FakeRuntime(),
            publish_event=lambda *_args, **_kwargs: None,
        )

        from agent.strategy_agent_tools import _get_store

        self.store = _get_store(self.session)
        self.store.save_strategy(_build_ir("momentum"), "momentum", 1, pool="candidates")
        self.server = DaemonServer(agent_name="kai", nats_url="nats://unit-test", bus_factory=None)
        self.managed = ManagedSession(session=self.session)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_daemon_optimizer_commands_parse_and_render(self) -> None:
        status = await self.server.handle_optimizer_command(self.managed, "/optimizer status")
        started = await self.server.handle_optimizer_command(self.managed, "/optimizer start 4")
        report = await self.server.handle_optimizer_command(self.managed, "/optimizer report")

        self.assertIn("Optimizer:", status)
        self.assertIn("4 cycle(s)", started)
        self.assertIn("Recent optimizer cycles", report)

    async def test_daemon_strategies_commands_parse_and_validate_usage(self) -> None:
        listed = await self.server.handle_strategies_command(self.managed, "/strategies list all")
        shown = await self.server.handle_strategies_command(self.managed, "/strategies show momentum")

        self.assertIn("Strategies (all):", listed)
        self.assertIn("momentum v1", shown)

        with self.assertRaisesRegex(ValueError, "usage: /strategies show NAME"):
            await self.server.handle_strategies_command(self.managed, "/strategies show")

    async def test_terminal_local_optimizer_and_strategies_commands_emit_messages(self) -> None:
        terminal = TradingTerminal.__new__(TradingTerminal)
        terminal.session = self.session
        terminal._chat_lines: list[str] = []
        terminal._chat_msg = terminal._chat_lines.append

        handled_optimizer = await terminal._handle_optimizer_command("/optimizer start 2")
        handled_strategies = await terminal._handle_strategies_command("/strategies list all")
        handled_error = await terminal._handle_strategies_command("/strategies show")

        self.assertTrue(handled_optimizer)
        self.assertTrue(handled_strategies)
        self.assertTrue(handled_error)
        self.assertTrue(any("Optimizer started" in line for line in terminal._chat_lines))
        self.assertTrue(any("Strategies (all):" in line for line in terminal._chat_lines))
        self.assertTrue(any("Usage: /strategies show NAME" in line for line in terminal._chat_lines))

    async def test_terminal_local_propose_accepts_file_path(self) -> None:
        terminal = TradingTerminal.__new__(TradingTerminal)
        terminal.session = self.session
        terminal._chat_lines: list[str] = []
        terminal._chat_msg = terminal._chat_lines.append

        yaml_path = Path(self.temp_dir.name) / "candidate.yaml"
        yaml_path.write_text(
            """
name: breakout
symbol: BTC-USD
timeframe: 1h
indicators:
  - type: sma
    period: 10
    alias: sma_fast
entry:
  conditions:
    - indicator: sma_fast
      operator: above
      value: 100
exit:
  time_exit:
    max_bars: 2
risk:
  max_position_pct: 0.1
  max_drawdown_pct: 0.2
""".strip(),
            encoding="utf-8",
        )

        handled = await terminal._handle_strategies_command(f"/strategies propose {yaml_path}")

        self.assertTrue(handled)
        self.assertTrue(any("Saved breakout v1" in line for line in terminal._chat_lines))


if __name__ == "__main__":
    unittest.main()
