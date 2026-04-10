export type ChatHistoryEntry = {
  role: string;
  content: string;
};

export type SessionStateSnapshot = {
  chart_symbol: string;
  chart_timeframe: string;
  chart_source: string;
  chart_layout_mode: string;
  chart_color_scheme: string;
  watchlist_symbols: string[];
  autotrade_enabled: boolean;
  activity_status: string;
  chat_history: ChatHistoryEntry[];
};

export type SessionSummary = {
  name: string;
  created_at?: string;
  last_activity?: string;
  activity_status?: string;
  queued_inputs?: number;
  state_path?: string;
};

export type WatchlistQuote = {
  symbol: string;
  price?: number;
  volume_24h?: number;
  price_change_24h_pct?: number;
  error?: string;
};

export type PositionRow = {
  symbol: string;
  side: string;
  quantity: number;
  entry_price: number;
  current_price: number;
  unrealized_pnl: number;
  pnl_pct: number;
};

export type PortfolioSnapshot = {
  positions: PositionRow[];
  pnl: {
    total_value?: number;
    total_pnl?: number;
    total_pnl_pct?: number;
    [key: string]: number | string | undefined;
  };
};

export type SessionAttachedEnvelope = {
  type: "session_attached";
  session: string;
  state: SessionStateSnapshot;
};

export type StatusEnvelope = {
  type: "status";
  activity: string;
  queue: number;
};

export type TokenEnvelope = {
  type: "token";
  text: string;
};

export type FinalEnvelope = {
  type: "final";
  text: string;
};

export type ErrorEnvelope = {
  type: "error";
  code: string;
  message: string;
};

export type ToolStartEnvelope = {
  type: "tool_start";
  tool: string;
  args?: unknown;
};

export type ToolEndEnvelope = {
  type: "tool_end";
  tool: string;
  elapsed_ms?: number | null;
  ok: boolean;
};

export type SignalEnvelope = {
  type: "signal";
  signal: Record<string, unknown>;
};

export type ChartBarEnvelope = {
  type: "chart_bar";
  symbol: string;
  tf: string;
  bar: Record<string, unknown>;
};

export type NatsEventEnvelope = {
  type: "nats_event";
  direction: string;
  subject: string;
  payload: Record<string, unknown>;
};

export type ScheduledJobEnvelope =
  | { type: "scheduled_job_created"; job: Record<string, unknown> }
  | { type: "scheduled_job_triggered"; job_id: string; fired_at: string }
  | { type: "scheduled_job_completed"; job_id: string; result_preview?: string | null }
  | { type: "scheduled_job_failed"; job_id: string; error: string }
  | { type: "scheduled_job_cancelled"; job_id: string }
  | { type: "scheduled_job_paused"; job_id: string }
  | { type: "scheduled_job_resumed"; job_id: string };

export type ServerEnvelope =
  | SessionAttachedEnvelope
  | StatusEnvelope
  | TokenEnvelope
  | FinalEnvelope
  | ErrorEnvelope
  | ToolStartEnvelope
  | ToolEndEnvelope
  | SignalEnvelope
  | ChartBarEnvelope
  | NatsEventEnvelope
  | ScheduledJobEnvelope;

export function isServerEnvelope(value: unknown): value is ServerEnvelope {
  if (!value || typeof value !== "object") {
    return false;
  }
  const envelope = value as { type?: unknown };
  return typeof envelope.type === "string" && envelope.type.length > 0;
}
