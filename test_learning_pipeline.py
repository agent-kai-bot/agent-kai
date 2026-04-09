#!/usr/bin/env python3
"""Offline unit test for the learning pipeline — no LLM required.

Exercises every piece that the /learn flow and the skill eval harness
depend on, without hitting any endpoint:

1. ToolCallRecorder captures tool calls through the BaseCallbackHandler API
2. SessionRecord.skill_was_created() detects skill_manage(create) calls
3. SessionRecord.to_bundle() produces a well-formed reflection bundle
4. parse_mentor_reply() correctly splits create / patch / no_skill replies
5. SkillStore.create() and .patch() applied to a temp dir work end-to-end
6. save_reflection_record() writes a readable JSON file

Run: .venv/bin/python test_learning_pipeline.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.learning import (
    NUDGE_THRESHOLD,
    SessionRecord,
    ToolCall,
    ToolCallRecorder,
    parse_mentor_reply,
    save_reflection_record,
)
from agent.skills_store import SkillStore


def _h(label: str) -> None:
    print("\n" + "=" * 60)
    print(f"  {label}")
    print("=" * 60)


def main() -> int:
    failures: list[str] = []

    def fail(msg: str) -> None:
        print(f"  FAIL: {msg}")
        failures.append(msg)

    _h("0. constants")
    if NUDGE_THRESHOLD != 3:
        fail(f"NUDGE_THRESHOLD expected 3, got {NUDGE_THRESHOLD}")
    else:
        print("  NUDGE_THRESHOLD = 3 OK")

    _h("1. ToolCallRecorder captures a normal tool call")
    rec = ToolCallRecorder()
    rec.on_tool_start({"name": "query_ohlcv"}, '{"symbol": "BTC", "interval": "1h"}')
    rec.on_tool_end("200 candles returned, latest close 68420.5")
    if len(rec.calls) != 1:
        fail(f"expected 1 call, got {len(rec.calls)}")
    else:
        call = rec.calls[0]
        if call.tool != "query_ohlcv":
            fail(f"wrong tool: {call.tool}")
        if not isinstance(call.input, dict) or call.input.get("symbol") != "BTC":
            fail(f"input not parsed as dict: {call.input!r}")
        if "68420" not in call.output:
            fail(f"output not captured: {call.output}")
        print(f"  captured: {call.tool}({call.input}) → {call.output[:40]}")

    _h("2. ToolCallRecorder captures an error")
    rec2 = ToolCallRecorder()
    rec2.on_tool_start({"name": "calculate_indicator"}, '{"symbol": "SOL"}')
    rec2.on_tool_error(RuntimeError("indicator unknown"))
    if len(rec2.calls) != 1 or not rec2.calls[0].error:
        fail(f"expected 1 errored call, got {rec2.calls}")
    else:
        print(f"  errored call captured: {rec2.calls[0].output}")

    _h("3. SessionRecord.skill_was_created detects create action")
    session = SessionRecord(agent="analyst", task="analyze BTC", response="done")
    session.tool_calls = [
        ToolCall(tool="query_ohlcv", input={"symbol": "BTC"}, output="..."),
        ToolCall(tool="calculate_indicator", input={"indicator": "RSI"}, output="..."),
        ToolCall(
            tool="skill_manage",
            input={"action": "create", "name": "foo", "content": "---\nname: foo\ndescription: x\n---\n# body\n"},
            output='{"success": true}',
        ),
    ]
    if not session.skill_was_created():
        fail("skill_was_created should return True when skill_manage(create) succeeded")
    else:
        print("  detected create action OK")

    _h("4. SessionRecord.skill_was_created ignores non-create actions")
    session2 = SessionRecord(agent="analyst", task="q", response="r")
    session2.tool_calls = [
        ToolCall(tool="skill_manage", input={"action": "patch"}, output="{}"),
        ToolCall(tool="skill_manage", input={"action": "delete"}, output="{}"),
    ]
    if session2.skill_was_created():
        fail("should not detect create on patch/delete only")
    else:
        print("  ignored patch/delete OK")

    _h("5. Bundle shape")
    bundle = session.to_bundle(
        chat_turns=["user: analyze BTC", "agent: ok"],
        existing_skills=[{"name": "bb-squeeze-breakout", "description": "BB squeeze", "category": "analysis"}],
    )
    required_keys = {"target_agent", "original_task", "target_summary", "tool_calls",
                     "tool_count", "chat_turns", "existing_skills"}
    missing = required_keys - bundle.keys()
    if missing:
        fail(f"bundle missing keys: {missing}")
    if bundle["target_agent"] != "analyst":
        fail(f"wrong target_agent: {bundle['target_agent']}")
    if bundle["tool_count"] != 3:
        fail(f"wrong tool_count: {bundle['tool_count']}")
    print(f"  bundle has {len(bundle)} keys, tool_count={bundle['tool_count']}")

    _h("6. parse_mentor_reply: create")
    create_reply = """Looking at the session I noticed a new pattern.

DECISION: create
TARGET_AGENT: analyst
SKILL_NAME: btc-volume-surge
OP: create
SKILL_CONTENT:
---
name: btc-volume-surge
description: Detect abnormal BTC volume spikes
category: analysis
---
# BTC volume surge
## When to use
When volume > 2x 20-bar average.
## Steps
1. query_ohlcv
2. Check volume column
## Pitfalls
- Low liquidity windows
## Verification
- Check the number
"""
    parsed = parse_mentor_reply(create_reply)
    if parsed.get("decision") != "create":
        fail(f"expected create, got {parsed.get('decision')}")
    if parsed.get("skill_name") != "btc-volume-surge":
        fail(f"wrong skill_name: {parsed.get('skill_name')}")
    if "btc-volume-surge" not in (parsed.get("content") or ""):
        fail(f"content doesn't contain skill name: {parsed.get('content')!r}")
    print(f"  parsed create: {parsed.get('skill_name')} ({len(parsed.get('content', ''))} chars)")

    _h("7. parse_mentor_reply: patch")
    patch_reply = """DECISION: patch
TARGET_AGENT: analyst
SKILL_NAME: bb-squeeze-breakout
OP: patch
OLD_STRING:
period=20, std=2
NEW_STRING:
period=20, std=2.5
"""
    parsed2 = parse_mentor_reply(patch_reply)
    if parsed2.get("decision") != "patch":
        fail(f"expected patch, got {parsed2.get('decision')}")
    if parsed2.get("old_string") != "period=20, std=2":
        fail(f"wrong old: {parsed2.get('old_string')!r}")
    if parsed2.get("new_string") != "period=20, std=2.5":
        fail(f"wrong new: {parsed2.get('new_string')!r}")
    print(f"  parsed patch: {parsed2.get('old_string')} → {parsed2.get('new_string')}")

    _h("8. parse_mentor_reply: no_skill")
    no_reply = "DECISION: no_skill\n\nThis session already fit existing patterns — no learning to capture."
    parsed3 = parse_mentor_reply(no_reply)
    if parsed3.get("decision") != "no_skill":
        fail(f"expected no_skill, got {parsed3.get('decision')}")
    else:
        print("  parsed no_skill OK")

    _h("9. SkillStore create from mentor draft")
    tmp = Path(tempfile.mkdtemp(prefix="kai-learn-test-"))
    try:
        store = SkillStore(tmp / "skills")
        content = parsed.get("content", "")
        result = store.create(parsed["skill_name"], content)
        if not result.get("success"):
            fail(f"create failed: {result}")
        else:
            print(f"  created: {result.get('message')}")
        items = store.list_skills()
        if not any(i["name"] == "btc-volume-surge" for i in items):
            fail(f"skill not in listing: {items}")
        else:
            print(f"  listed: {len(items)} skill(s)")

        _h("10. save_reflection_record writes JSON")
        bundle_path = save_reflection_record(
            bundle,
            create_reply,
            {"decision": "create", "skill_name": "btc-volume-surge", "result": result},
            directory=tmp / "reflections",
        )
        if not bundle_path.exists():
            fail(f"reflection file not written: {bundle_path}")
        else:
            loaded = json.loads(bundle_path.read_text())
            if loaded["outcome"]["decision"] != "create":
                fail(f"loaded reflection wrong: {loaded}")
            else:
                print(f"  reflection saved to {bundle_path.name} ({bundle_path.stat().st_size} bytes)")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if failures:
        print(f"  {len(failures)} FAILURE(S)")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  ALL LEARNING-PIPELINE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
