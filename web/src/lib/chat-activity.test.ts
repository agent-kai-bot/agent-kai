import {
  applyChatActivityEnvelope,
  emptyChatActivityState,
  formatToolElapsed,
  startChatActivityTurn,
} from "$lib/chat-activity";

describe("chat activity helpers", () => {
  it("formats tool elapsed time compactly", () => {
    expect(formatToolElapsed(480)).toBe("480ms");
    expect(formatToolElapsed(1200)).toBe("1.2s");
    expect(formatToolElapsed(10_000)).toBe("10s");
    expect(formatToolElapsed(60_000)).toBe("60s");
    expect(formatToolElapsed(63_000)).toBe("1m 03s");
  });

  it("tracks overlapping tool starts and clears when the turn finalizes", () => {
    let state = startChatActivityTurn(new Date("2026-05-12T14:32:05Z"));

    state = applyChatActivityEnvelope(state, {
      type: "tool_start",
      tool: "file_read",
      args: "/tmp/report.txt",
    });
    state = applyChatActivityEnvelope(state, {
      type: "tool_start",
      tool: "python_exec",
      args: { code: "print(1)" },
    });

    expect(state.tools).toMatchObject([
      { tool: "file_read", state: "running" },
      { tool: "python_exec", state: "running" },
    ]);

    state = applyChatActivityEnvelope(state, {
      type: "tool_end",
      tool: "file_read",
      elapsed_ms: 1200,
      ok: true,
    });

    expect(state.tools).toMatchObject([
      { tool: "file_read", state: "complete", elapsedMs: 1200, ok: true },
      { tool: "python_exec", state: "running" },
    ]);

    state = applyChatActivityEnvelope(state, { type: "final", text: "done" });

    expect(state).toEqual(emptyChatActivityState());
  });

  it("updates auto-mode badge state through start, progress, and stopped events", () => {
    let state = emptyChatActivityState();

    state = applyChatActivityEnvelope(state, {
      type: "auto_started",
      readonly: false,
      iterations_total: 5,
      iterations_remaining: 4,
      iterations_used: 1,
      elapsed_seconds: 3,
    });

    expect(state.active).toBe(true);
    expect(state.auto).toMatchObject({
      iterationsTotal: 5,
      iterationsUsed: 1,
      elapsedSeconds: 3,
      status: "running",
    });

    state = applyChatActivityEnvelope(state, {
      type: "auto_progress",
      readonly: false,
      iterations_total: 5,
      iterations_remaining: 3,
      iterations_used: 2,
      elapsed_seconds: 31,
    });

    expect(state.auto).toMatchObject({
      iterationsTotal: 5,
      iterationsUsed: 2,
      elapsedSeconds: 31,
      status: "running",
    });

    state = applyChatActivityEnvelope(state, {
      type: "auto_stopped",
      readonly: false,
      iterations_total: 5,
      iterations_remaining: 2,
      iterations_used: 3,
      elapsed_seconds: 47,
      reason: "operator stopped",
    });

    expect(state.auto).toMatchObject({
      iterationsTotal: 5,
      iterationsUsed: 3,
      elapsedSeconds: 47,
      status: "stopped",
      reason: "operator stopped",
    });
  });
});
