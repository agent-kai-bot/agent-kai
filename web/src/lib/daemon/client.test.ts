import { DaemonClient, buildAuthHeaders, buildWebSocketUrl } from "$lib/daemon/client";
import { readStoredToken, writeStoredToken } from "$lib/daemon/storage";

class FakeWebSocket {
  readonly url: string;
  sent: string[] = [];
  private listeners: Record<string, Array<(event?: { data: string; code?: number }) => void>> = {};

  constructor(url: string) {
    this.url = url;
  }

  addEventListener(type: "open", listener: () => void): void;
  addEventListener(type: "message", listener: (event: { data: string }) => void): void;
  addEventListener(type: "error", listener: () => void): void;
  addEventListener(type: "close", listener: (event: { code?: number }) => void): void;
  addEventListener(
    type: "open" | "message" | "error" | "close",
    listener:
      | (() => void)
      | ((event: { data: string }) => void)
      | ((event: { code?: number }) => void),
  ): void {
    this.listeners[type] ??= [];
    this.listeners[type]?.push(listener as (event?: { data: string; code?: number }) => void);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code?: number): void {
    this.emit("close", { data: "", code });
  }

  emit(type: "open" | "message" | "error" | "close", event?: { data: string; code?: number }): void {
    for (const listener of this.listeners[type] ?? []) {
      listener(event);
    }
  }
}

describe("daemon client helpers", () => {
  it("builds websocket URLs with a token query for browser auth", () => {
    expect(buildWebSocketUrl("https://kai.example.com", "secret-token")).toBe(
      "wss://kai.example.com/ws?token=secret-token",
    );
    expect(buildWebSocketUrl("http://127.0.0.1:8765", "")).toBe(
      "ws://127.0.0.1:8765/ws",
    );
  });

  it("emits bearer headers only when a token is present", () => {
    expect(buildAuthHeaders("")).toBeUndefined();
    expect(buildAuthHeaders(" token ")).toEqual({
      Authorization: "Bearer token",
    });
  });

  it("persists the daemon token in local storage", () => {
    writeStoredToken("phase-6");
    expect(readStoredToken()).toBe("phase-6");

    writeStoredToken("");
    expect(readStoredToken()).toBe("");
  });
});

describe("daemon attach handshake", () => {
  it("sends attach and resolves after the initial status envelope", async () => {
    let socket: FakeWebSocket | undefined;
    const client = new DaemonClient({
      baseHttpUrl: "http://127.0.0.1:8765",
      fetchImpl: vi.fn(),
      webSocketFactory: (url) => {
        socket = new FakeWebSocket(url);
        queueMicrotask(() => {
          socket?.emit("open");
          socket?.emit("message", {
            data: JSON.stringify({
              type: "session_attached",
              session: "terminal",
              state: {
                chart_symbol: "BTC",
                chart_timeframe: "1m",
                chart_source: "kai-api",
                chart_layout_mode: "dashboard",
                chart_color_scheme: "classic",
                watchlist_symbols: ["BTC", "ETH"],
                autotrade_enabled: false,
                activity_status: "idle",
                chat_history: [],
              },
            }),
          });
          socket?.emit("message", {
            data: JSON.stringify({
              type: "status",
              activity: "idle",
              queue: 0,
            }),
          });
        });
        return socket;
      },
    });

    const connection = await client.attach({
      session: "terminal",
      token: "demo-token",
      createIfMissing: true,
    });

    expect(socket).toBeDefined();
    const attachedSocket = socket as FakeWebSocket;

    expect(attachedSocket.url).toBe("ws://127.0.0.1:8765/ws?token=demo-token");
    expect(attachedSocket.sent).toHaveLength(1);
    expect(JSON.parse(attachedSocket.sent[0] ?? "{}")).toMatchObject({
      type: "attach",
      session: "terminal",
      create_if_missing: true,
    });
    expect(connection.session).toBe("terminal");
    expect(connection.snapshot.chart_symbol).toBe("BTC");
  });
});
