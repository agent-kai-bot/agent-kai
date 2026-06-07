import {
  applyMobileEnvelope,
  emptyMobileChatState,
  mobileChatStateFromSnapshot,
  normalizeDaemonBaseUrl,
  parseOutgoingMobileMessage,
  startMobileUserTurn,
} from "$lib/mobile-chat";
import type { SessionStateSnapshot } from "$lib/daemon/types";

const snapshot: SessionStateSnapshot = {
  chart_symbol: "BTC",
  chart_timeframe: "1m",
  chart_source: "kai-api",
  chart_layout_mode: "dashboard",
  chart_color_scheme: "classic",
  watchlist_symbols: ["BTC"],
  autotrade_enabled: false,
  activity_status: "idle",
  chat_history_total: 3,
  chat_history_omitted: 1,
  chat_history: [
    {
      role: "human",
      content: "status",
      ts: "2026-05-26T12:00:00Z",
    },
    {
      role: "ai",
      content: "idle",
      ts: "2026-05-26T12:00:01Z",
    },
  ],
};

describe("mobile chat state", () => {
  it("hydrates recent chat history from a daemon attach snapshot", () => {
    const state = mobileChatStateFromSnapshot(snapshot, {
      session: "terminal",
      status: "idle",
      queueDepth: 0,
    });

    expect(state.attachedSession).toBe("terminal");
    expect(state.messages.map((message) => message.content)).toEqual([
      "status",
      "idle",
    ]);
    expect(state.historyLabel).toBe("2/3 recent messages");
  });

  it("tracks streamed tokens and replaces them with the final reply", () => {
    const now = new Date("2026-05-26T12:01:00Z");
    let state = startMobileUserTurn(emptyMobileChatState(), "hello", now);

    state = applyMobileEnvelope(state, { type: "token", text: "he" }, now);
    state = applyMobileEnvelope(state, { type: "token", text: "llo" }, now);
    expect(state.streamingReply).toBe("hello");
    expect(state.activity.active).toBe(true);

    state = applyMobileEnvelope(state, { type: "final", text: "hello back" }, now);
    expect(state.streamingReply).toBe("");
    expect(state.activity.active).toBe(false);
    expect(state.historyLabel).toBe("2 messages");
    expect(state.messages.map((message) => message.content)).toEqual([
      "hello",
      "hello back",
    ]);
  });

  it("parses slash commands separately from normal input", () => {
    expect(parseOutgoingMobileMessage("check BTC")).toEqual({
      kind: "input",
      text: "check BTC",
    });
    expect(parseOutgoingMobileMessage(" /schedule list ")).toEqual({
      kind: "slash",
      text: "/schedule list",
      command: "/schedule",
      args: "list",
    });
  });

  it("normalizes daemon URLs for REST and WSS derivation", () => {
    expect(normalizeDaemonBaseUrl("kai.example.com", "https://app.example.com")).toBe(
      "https://kai.example.com",
    );
    expect(normalizeDaemonBaseUrl("http://192.168.1.20:8765/mobile")).toBe(
      "http://192.168.1.20:8765",
    );
  });
});
