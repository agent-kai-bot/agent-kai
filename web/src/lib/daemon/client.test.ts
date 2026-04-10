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

  it("loads sidebar snapshots through the daemon REST API", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ quotes: [{ symbol: "BTC", price: 100_000 }] })),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            positions: [{ symbol: "BTC", side: "long", quantity: 1, entry_price: 90_000, current_price: 100_000, unrealized_pnl: 10_000, pnl_pct: 11.1 }],
            pnl: { total_value: 110_000 },
          }),
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            bars: [
              {
                ts: "2026-04-10T00:00:00Z",
                open: 1,
                high: 2,
                low: 0.5,
                close: 1.5,
                volume: 42,
              },
            ],
          }),
        ),
      );

    const client = new DaemonClient({
      baseHttpUrl: "http://127.0.0.1:8765",
      fetchImpl,
      webSocketFactory: vi.fn() as never,
    });

    const quotes = await client.fetchWatchlistQuotes(["BTC", "btc"], "secret");
    const portfolio = await client.fetchPortfolio("secret");
    const bars = await client.fetchChartHistory({
      symbol: "BTC",
      interval: "1h",
      source: "coinbase",
      token: "secret",
    });

    expect(quotes).toEqual([{ symbol: "BTC", price: 100_000 }]);
    expect(portfolio.positions).toHaveLength(1);
    expect(bars[0]?.close).toBe(1.5);
    expect(fetchImpl).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8765/api/market/watchlist?symbols=BTC",
      { headers: { Authorization: "Bearer secret" } },
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8765/api/portfolio",
      { headers: { Authorization: "Bearer secret" } },
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8765/api/market/ohlcv?symbol=BTC&interval=1h&source=coinbase&limit=300",
      { headers: { Authorization: "Bearer secret" } },
    );
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
