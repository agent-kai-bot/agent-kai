import type {
  CandleBar,
  PortfolioSnapshot,
  ServerEnvelope,
  SessionAttachedEnvelope,
  SessionSummary,
  StatusEnvelope,
  WatchlistQuote,
} from "$lib/daemon/types";

export const DEFAULT_HTTP_BASE_URL = "http://127.0.0.1:8765";
export const DEFAULT_SESSION_NAME = "terminal";

type FetchLike = typeof fetch;
type WebSocketFactory = (url: string) => WebSocketLike;

interface WebSocketLike {
  addEventListener(type: "open", listener: () => void): void;
  addEventListener(type: "message", listener: (event: { data: string }) => void): void;
  addEventListener(type: "error", listener: () => void): void;
  addEventListener(type: "close", listener: (event: { code?: number }) => void): void;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export type AttachOptions = {
  session: string;
  token?: string;
  createIfMissing?: boolean;
};

type JsonRecord = Record<string, unknown>;

export function deriveHttpBaseUrl(): string {
  if (typeof window === "undefined") {
    return DEFAULT_HTTP_BASE_URL;
  }
  return `${window.location.protocol}//${window.location.host}`;
}

export function buildAuthHeaders(token: string): HeadersInit | undefined {
  const normalized = token.trim();
  if (!normalized) {
    return undefined;
  }
  return { Authorization: `Bearer ${normalized}` };
}

export function buildWebSocketUrl(baseHttpUrl: string, token: string): string {
  const url = new URL(baseHttpUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws";
  url.search = "";
  const normalized = token.trim();
  if (normalized) {
    url.searchParams.set("token", normalized);
  }
  return url.toString();
}

function normalizeSessionName(session: string): string {
  const normalized = session.trim();
  return normalized || DEFAULT_SESSION_NAME;
}

function parseEnvelope(raw: string): ServerEnvelope {
  const parsed = JSON.parse(raw) as unknown;
  if (!parsed || typeof parsed !== "object" || typeof (parsed as { type?: unknown }).type !== "string") {
    throw new Error("daemon sent an invalid envelope");
  }
  return parsed as ServerEnvelope;
}

function dedupeSymbols(symbols: string[]): string[] {
  const seen = new Set<string>();
  const normalized: string[] = [];
  for (const rawSymbol of symbols) {
    const symbol = rawSymbol.trim().toUpperCase();
    if (!symbol || seen.has(symbol)) {
      continue;
    }
    seen.add(symbol);
    normalized.push(symbol);
  }
  return normalized;
}

export class DaemonConnection {
  readonly socket: WebSocketLike;
  readonly session: string;
  snapshot: SessionAttachedEnvelope["state"];
  activityStatus: string;
  queueDepth: number;
  onEnvelope?: (envelope: ServerEnvelope) => void;
  onClose?: (code?: number) => void;

  constructor(
    socket: WebSocketLike,
    attached: SessionAttachedEnvelope,
    initialStatus: StatusEnvelope,
  ) {
    this.socket = socket;
    this.session = attached.session;
    this.snapshot = attached.state;
    this.activityStatus = initialStatus.activity;
    this.queueDepth = initialStatus.queue;
  }

  sendInput(text: string): void {
    this.socket.send(
      JSON.stringify({
        type: "input",
        text,
      }),
    );
  }

  sendSlash(command: string, args = ""): void {
    this.socket.send(
      JSON.stringify({
        type: "slash",
        command,
        args,
      }),
    );
  }

  subscribe(channel: "chart" | "signals" | "nats", symbol?: string, tf?: string): void {
    this.socket.send(
      JSON.stringify({
        type: "subscribe",
        channel,
        symbol,
        tf,
      }),
    );
  }

  close(code?: number, reason?: string): void {
    this.socket.close(code, reason);
  }

  handleEnvelope(envelope: ServerEnvelope): void {
    if (envelope.type === "session_attached") {
      this.snapshot = envelope.state;
    } else if (envelope.type === "status") {
      this.activityStatus = envelope.activity;
      this.queueDepth = envelope.queue;
    }
    this.onEnvelope?.(envelope);
  }
}

export class DaemonClient {
  private readonly baseHttpUrl: string;
  private readonly fetchImpl: FetchLike;
  private readonly webSocketFactory: WebSocketFactory;

  constructor(options?: {
    baseHttpUrl?: string;
    fetchImpl?: FetchLike;
    webSocketFactory?: WebSocketFactory;
  }) {
    this.baseHttpUrl = options?.baseHttpUrl ?? deriveHttpBaseUrl();
    this.fetchImpl = options?.fetchImpl ?? fetch;
    this.webSocketFactory =
      options?.webSocketFactory ?? ((url: string) => new WebSocket(url));
  }

  private async requestJson(path: string, token = ""): Promise<JsonRecord> {
    const response = await this.fetchImpl(`${this.baseHttpUrl}${path}`, {
      headers: buildAuthHeaders(token),
    });
    if (!response.ok) {
      throw new Error(`${path} failed (${response.status})`);
    }
    const payload = (await response.json()) as unknown;
    return payload && typeof payload === "object" ? (payload as JsonRecord) : {};
  }

  async listSessions(token = ""): Promise<SessionSummary[]> {
    const payload = (await this.requestJson("/api/sessions", token)) as {
      sessions?: SessionSummary[];
    };
    return Array.isArray(payload.sessions) ? payload.sessions : [];
  }

  async fetchWatchlistQuotes(
    symbols: string[],
    token = "",
  ): Promise<WatchlistQuote[]> {
    const normalized = dedupeSymbols(symbols);
    if (!normalized.length) {
      return [];
    }
    const params = new URLSearchParams({ symbols: normalized.join(",") });
    const payload = (await this.requestJson(
      `/api/market/watchlist?${params.toString()}`,
      token,
    )) as { quotes?: WatchlistQuote[] };
    return Array.isArray(payload.quotes) ? payload.quotes : [];
  }

  async fetchPortfolio(token = ""): Promise<PortfolioSnapshot> {
    const payload = (await this.requestJson("/api/portfolio", token)) as Partial<PortfolioSnapshot>;
    return {
      positions: Array.isArray(payload.positions) ? payload.positions : [],
      pnl: payload.pnl && typeof payload.pnl === "object" ? payload.pnl : {},
    };
  }

  async fetchChartHistory(options: {
    symbol: string;
    interval: string;
    source: string;
    token?: string;
    limit?: number;
  }): Promise<CandleBar[]> {
    const params = new URLSearchParams({
      symbol: options.symbol,
      interval: options.interval,
      source: options.source,
      limit: String(options.limit ?? 300),
    });
    const payload = (await this.requestJson(
      `/api/market/ohlcv?${params.toString()}`,
      options.token ?? "",
    )) as { bars?: CandleBar[] };
    return Array.isArray(payload.bars) ? payload.bars : [];
  }

  async attach(options: AttachOptions): Promise<DaemonConnection> {
    const session = normalizeSessionName(options.session);
    const socket = this.webSocketFactory(
      buildWebSocketUrl(this.baseHttpUrl, options.token ?? ""),
    );

    return await new Promise<DaemonConnection>((resolve, reject) => {
      let attached: SessionAttachedEnvelope | null = null;
      let resolved = false;
      let connection: DaemonConnection | null = null;

      const rejectOnce = (message: string): void => {
        if (resolved) {
          return;
        }
        resolved = true;
        reject(new Error(message));
      };

      socket.addEventListener("open", () => {
        socket.send(
          JSON.stringify({
            type: "attach",
            session,
            create_if_missing: options.createIfMissing ?? true,
          }),
        );
      });

      socket.addEventListener("message", (event) => {
        try {
          const envelope = parseEnvelope(event.data);
          if (envelope.type === "error" && !resolved) {
            rejectOnce(envelope.message);
            return;
          }

          if (!attached) {
            if (envelope.type !== "session_attached") {
              rejectOnce("daemon did not acknowledge the session attach");
              return;
            }
            attached = envelope;
            return;
          }

          if (!connection) {
            if (envelope.type !== "status") {
              rejectOnce("daemon did not send initial status after attach");
              return;
            }
            connection = new DaemonConnection(socket, attached, envelope);
            connection.onEnvelope = undefined;
            connection.onClose = undefined;
            socket.addEventListener("message", (laterEvent) => {
              if (!connection) {
                return;
              }
              const laterEnvelope = parseEnvelope(laterEvent.data);
              if (laterEnvelope.type === "error" && !resolved) {
                rejectOnce(laterEnvelope.message);
                return;
              }
              connection.handleEnvelope(laterEnvelope);
            });
            socket.addEventListener("close", (closeEvent) => {
              connection?.onClose?.(closeEvent.code);
            });
            resolved = true;
            resolve(connection);
            return;
          }
        } catch (error) {
          rejectOnce(error instanceof Error ? error.message : String(error));
        }
      });

      socket.addEventListener("error", () => {
        rejectOnce("daemon websocket connection failed");
      });

      socket.addEventListener("close", (event) => {
        if (!resolved) {
          rejectOnce(`daemon websocket closed (${event.code ?? 1000})`);
        } else {
          connection?.onClose?.(event.code);
        }
      });
    });
  }
}
