import type {
  AutoLoopBrainConfig,
  AutoLoopBrainHealth,
  CandleBar,
  ChartViewPatch,
  ChartViewResponse,
  WatchlistPatch,
  WatchlistResponse,
  ModelRegistryResponse,
  ModelSwitchResponse,
  PortfolioSnapshot,
  ServerEnvelope,
  SessionAttachedEnvelope,
  SessionSummary,
  SignalRouterConfig,
  SignalRouterHealth,
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

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function normalizeAutoLoopBrainHealth(payload: Partial<AutoLoopBrainHealth>): AutoLoopBrainHealth {
  return {
    enabled: asBoolean(payload.enabled),
    effective_client: asString(payload.effective_client, "unknown"),
    effective_model: asString(payload.effective_model, "unknown"),
    kill_switch_active: asBoolean(payload.kill_switch_active),
    boot_probe_last_at: payload.boot_probe_last_at ?? null,
    boot_probe_last_ok: typeof payload.boot_probe_last_ok === "boolean"
      ? payload.boot_probe_last_ok
      : null,
    calls_total: asNumber(payload.calls_total),
    escalations_total: asNumber(payload.escalations_total),
  };
}

function normalizeAutoLoopBrainConfig(payload: Partial<AutoLoopBrainConfig>): AutoLoopBrainConfig {
  return {
    enabled: asBoolean(payload.enabled),
    client: asString(payload.client, "unknown"),
    endpoint: payload.endpoint ?? null,
    model_id: asString(payload.model_id, "unknown"),
    codex_reasoning_effort: asString(payload.codex_reasoning_effort, "medium"),
    max_history_tokens: asNumber(payload.max_history_tokens),
    temperature: asNumber(payload.temperature),
    min_continue_confidence: asNumber(payload.min_continue_confidence),
    timeout_seconds: asNumber(payload.timeout_seconds),
    max_output_tokens: asNumber(payload.max_output_tokens),
    max_llm_critic_calls_per_session: asNumber(payload.max_llm_critic_calls_per_session),
    max_consecutive_llm_critic_calls: asNumber(payload.max_consecutive_llm_critic_calls),
    kill_switch_active: asBoolean(payload.kill_switch_active),
  };
}

function normalizeSignalRouterConfig(payload: Partial<SignalRouterConfig>): SignalRouterConfig {
  return {
    mode: payload.mode ?? "legacy",
    live_trades_enabled: asBoolean(payload.live_trades_enabled),
    kill_switch_active: asBoolean(payload.kill_switch_active),
    routes: Array.isArray(payload.routes) ? payload.routes : [],
    last_decisions: Array.isArray(payload.last_decisions) ? payload.last_decisions : [],
    dedup_stats: payload.dedup_stats ?? {
      keys_count: 0,
      cooldown_hits_24h: 0,
      cap_hits_24h: 0,
    },
  };
}

function normalizeSignalRouterHealth(payload: Partial<SignalRouterHealth>): SignalRouterHealth {
  return {
    mode: payload.mode ?? "legacy",
    routes_loaded: asNumber(payload.routes_loaded),
    channels_loaded: asNumber(payload.channels_loaded),
    dedup_keys_count: asNumber(payload.dedup_keys_count),
    kill_switch_active: asBoolean(payload.kill_switch_active),
    live_trades_enabled: asBoolean(payload.live_trades_enabled),
    routes_enabled_count: asNumber(payload.routes_enabled_count),
    routes_disabled_count: asNumber(payload.routes_disabled_count),
    shadow_running: asBoolean(payload.shadow_running),
    diff_metrics: payload.diff_metrics ?? {},
  };
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

  interrupt(): void {
    this.socket.send(JSON.stringify({ type: "interrupt" }));
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

  unsubscribe(channel: "chart" | "signals" | "nats", symbol?: string, tf?: string): void {
    this.socket.send(
      JSON.stringify({
        type: "unsubscribe",
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
    } else if (envelope.type === "chart_view") {
      this.snapshot.chart_symbol = envelope.chart_symbol;
      this.snapshot.chart_timeframe = envelope.chart_timeframe;
      this.snapshot.chart_source = envelope.chart_source;
      this.snapshot.chart_layout_mode = envelope.chart_layout_mode;
    } else if (envelope.type === "watchlist") {
      this.snapshot.watchlist_symbols = envelope.watchlist_symbols;
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

  private async requestJson(
    path: string,
    token = "",
    init: RequestInit = {},
  ): Promise<JsonRecord> {
    const headers = {
      ...(buildAuthHeaders(token) ?? {}),
      ...(init.headers ?? {}),
    };
    const response = await this.fetchImpl(`${this.baseHttpUrl}${path}`, {
      ...init,
      headers,
    });
    if (!response.ok) {
      throw new Error(`${path} failed (${response.status})`);
    }
    const payload = (await response.json()) as unknown;
    return payload && typeof payload === "object" ? (payload as JsonRecord) : {};
  }

  private async postJson(
    path: string,
    payload: JsonRecord,
    token = "",
  ): Promise<JsonRecord> {
    return this.requestJson(path, token, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  private async postEmpty(path: string, token = ""): Promise<JsonRecord> {
    const response = await this.fetchImpl(`${this.baseHttpUrl}${path}`, {
      method: "POST",
      headers: buildAuthHeaders(token),
    });
    if (!response.ok) {
      throw new Error(`${path} failed (${response.status})`);
    }
    const payload = (await response.json()) as unknown;
    return payload && typeof payload === "object" ? (payload as JsonRecord) : {};
  }

  private async patchJson(
    path: string,
    payload: JsonRecord,
    token = "",
  ): Promise<JsonRecord> {
    return this.requestJson(path, token, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  async listSessions(token = ""): Promise<SessionSummary[]> {
    const payload = (await this.requestJson("/api/sessions", token)) as {
      sessions?: SessionSummary[];
    };
    return Array.isArray(payload.sessions) ? payload.sessions : [];
  }

  async fetchModelRegistry(token = ""): Promise<ModelRegistryResponse> {
    const payload = (await this.requestJson("/api/models", token)) as Partial<ModelRegistryResponse>;
    return {
      agents: Array.isArray(payload.agents) ? payload.agents : [],
      endpoints: Array.isArray(payload.endpoints) ? payload.endpoints : [],
    };
  }

  async fetchAutoLoopBrainHealth(token = ""): Promise<AutoLoopBrainHealth> {
    const payload = (await this.requestJson(
      "/api/health.auto_loop_brain",
      token,
    )) as Partial<AutoLoopBrainHealth>;
    return normalizeAutoLoopBrainHealth(payload);
  }

  async fetchAutoLoopBrainConfig(token = ""): Promise<AutoLoopBrainConfig> {
    const payload = (await this.requestJson(
      "/api/daemon/config/auto_loop_brain",
      token,
    )) as Partial<AutoLoopBrainConfig>;
    return normalizeAutoLoopBrainConfig(payload);
  }

  async updateAutoLoopBrainConfig(
    enabled: boolean,
    token = "",
  ): Promise<AutoLoopBrainConfig> {
    const payload = (await this.patchJson(
      "/api/daemon/config/auto_loop_brain",
      { enabled },
      token,
    )) as Partial<AutoLoopBrainConfig>;
    return normalizeAutoLoopBrainConfig(payload);
  }

  async fetchSignalRouterHealth(token = ""): Promise<SignalRouterHealth> {
    const payload = (await this.requestJson(
      "/api/health.signal_router",
      token,
    )) as Partial<SignalRouterHealth>;
    return normalizeSignalRouterHealth(payload);
  }

  async fetchSignalRouterConfig(token = ""): Promise<SignalRouterConfig> {
    const payload = (await this.requestJson(
      "/api/daemon/config/signal_router",
      token,
    )) as Partial<SignalRouterConfig>;
    return normalizeSignalRouterConfig(payload);
  }

  async updateSignalRouterLiveTrades(
    liveTradesEnabled: boolean,
    token = "",
  ): Promise<SignalRouterConfig> {
    const payload = (await this.requestJson(
      "/api/daemon/config/signal_router",
      token,
      {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-Operator-Source": "operator-ui",
        },
        body: JSON.stringify({ live_trades_enabled: liveTradesEnabled }),
      },
    )) as Partial<SignalRouterConfig>;
    return normalizeSignalRouterConfig(payload);
  }

  async updateSignalRouterRoute(
    routeName: string,
    enabled: boolean,
    token = "",
  ): Promise<SignalRouterConfig> {
    const payload = (await this.requestJson(
      "/api/daemon/config/signal_router",
      token,
      {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-Operator-Source": "operator-ui",
        },
        body: JSON.stringify({ routes: { [routeName]: { enabled } } }),
      },
    )) as Partial<SignalRouterConfig>;
    return normalizeSignalRouterConfig(payload);
  }

  async switchAgentModel(
    agentName: string,
    endpoint: string,
    model: string,
    token = "",
    reasoningEffort?: string,
  ): Promise<ModelSwitchResponse> {
    const body = reasoningEffort
      ? { endpoint, model, reasoning_effort: reasoningEffort }
      : { endpoint, model };
    const payload = (await this.postJson(
      `/api/models/${encodeURIComponent(agentName)}`,
      body,
      token,
    )) as Partial<ModelSwitchResponse>;
    return {
      agent: payload.agent ?? { name: agentName, endpoint, model },
      reloaded_sessions: Array.isArray(payload.reloaded_sessions)
        ? payload.reloaded_sessions
        : [],
    };
  }

  async stopSession(sessionName: string, token = ""): Promise<JsonRecord> {
    return this.postEmpty(
      `/api/sessions/${encodeURIComponent(sessionName)}/stop`,
      token,
    );
  }

  async fetchChartView(
    sessionName: string,
    token = "",
  ): Promise<ChartViewResponse> {
    const payload = (await this.requestJson(
      `/api/sessions/${encodeURIComponent(sessionName)}/ui/chart`,
      token,
    )) as Partial<ChartViewResponse>;
    return {
      session: payload.session ?? sessionName,
      chart: payload.chart ?? {
        chart_symbol: "BTC",
        chart_timeframe: "1m",
        chart_source: "kai-api",
        chart_layout_mode: "full",
      },
    };
  }

  async updateChartView(
    sessionName: string,
    patch: ChartViewPatch,
    token = "",
  ): Promise<ChartViewResponse> {
    const payload = (await this.patchJson(
      `/api/sessions/${encodeURIComponent(sessionName)}/ui/chart`,
      patch,
      token,
    )) as Partial<ChartViewResponse>;
    return {
      session: payload.session ?? sessionName,
      chart: payload.chart ?? {
        chart_symbol: "BTC",
        chart_timeframe: "1m",
        chart_source: "kai-api",
        chart_layout_mode: "full",
      },
    };
  }

  async fetchSessionWatchlist(
    sessionName: string,
    token = "",
  ): Promise<WatchlistResponse> {
    const payload = (await this.requestJson(
      `/api/sessions/${encodeURIComponent(sessionName)}/ui/watchlist`,
      token,
    )) as Partial<WatchlistResponse>;
    return {
      session: payload.session ?? sessionName,
      watchlist: payload.watchlist ?? { watchlist_symbols: [] },
    };
  }

  async updateSessionWatchlist(
    sessionName: string,
    patch: WatchlistPatch,
    token = "",
  ): Promise<WatchlistResponse> {
    const payload = (await this.patchJson(
      `/api/sessions/${encodeURIComponent(sessionName)}/ui/watchlist`,
      patch,
      token,
    )) as Partial<WatchlistResponse>;
    return {
      session: payload.session ?? sessionName,
      watchlist: payload.watchlist ?? { watchlist_symbols: [] },
    };
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
