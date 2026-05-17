export type ChatHistoryEntry = {
  role: string;
  content: string;
  ts?: string;
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
  chat_history_total?: number;
  chat_history_omitted?: number;
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

export type CandleBar = {
  ts: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
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

export type ModelFallbackSummary = {
  endpoint?: string | null;
  model?: string | null;
  provider?: string | null;
  base_url?: string | null;
};

export type ModelAgentSummary = {
  name: string;
  description?: string;
  endpoint?: string | null;
  model?: string | null;
  provider?: string | null;
  base_url?: string | null;
  reasoning_effort?: string | null;
  text_verbosity?: string | null;
  max_iterations?: number | null;
  fallbacks?: ModelFallbackSummary[];
};

export type EndpointModelSummary = {
  name: string;
  provider?: string;
  base_url?: string;
  default_model?: string | null;
  models: string[];
};

export type ModelRegistryResponse = {
  agents: ModelAgentSummary[];
  endpoints: EndpointModelSummary[];
};

export type ModelSwitchResponse = {
  agent: ModelAgentSummary;
  reloaded_sessions: Array<{
    session: string;
    model?: string | null;
    provider?: string | null;
    reasoning_effort?: string | null;
    fallback_count?: number;
  }>;
};

export type AutoLoopBrainConfig = {
  enabled: boolean;
  client: string;
  endpoint?: string | null;
  model_id: string;
  codex_reasoning_effort?: string;
  max_history_tokens?: number;
  temperature?: number;
  min_continue_confidence?: number;
  timeout_seconds?: number;
  max_output_tokens?: number;
  max_llm_critic_calls_per_session?: number;
  max_consecutive_llm_critic_calls?: number;
  kill_switch_active?: boolean;
};

export type AutoLoopBrainHealth = {
  enabled: boolean;
  effective_client: string;
  effective_model: string;
  kill_switch_active: boolean;
  boot_probe_last_at?: string | null;
  boot_probe_last_ok?: boolean | null;
  calls_total: number;
  escalations_total: number;
};

export type SignalRouterDecision = {
  route: string;
  channel?: string | null;
  kind: string;
  timestamp: string;
  status: string;
  detail?: string | null;
};

export type SignalRouterAction = {
  kind: string;
  target?: string | null;
  [key: string]: unknown;
};

export type SignalRouterRoute = {
  name: string;
  channel: string;
  actions: SignalRouterAction[];
  enabled: boolean;
  fire_count_24h: number;
  suppress_count_24h: number;
  last_decisions: SignalRouterDecision[];
};

export type SignalRouterDedupStats = {
  keys_count: number;
  cooldown_hits_24h: number;
  cap_hits_24h: number;
};

export type SignalRouterConfig = {
  mode: "legacy" | "shadow" | "new";
  live_trades_enabled: boolean;
  kill_switch_active: boolean;
  routes: SignalRouterRoute[];
  last_decisions?: SignalRouterDecision[];
  dedup_stats: SignalRouterDedupStats;
};

export type SignalRouterHealth = {
  mode: "legacy" | "shadow" | "new";
  routes_loaded: number;
  channels_loaded: number;
  dedup_keys_count: number;
  kill_switch_active: boolean;
  live_trades_enabled: boolean;
  routes_enabled_count: number;
  routes_disabled_count: number;
  shadow_running?: boolean;
  diff_metrics?: Record<string, unknown>;
};

export type ChartViewState = {
  chart_symbol: string;
  chart_timeframe: string;
  chart_source: string;
  chart_layout_mode: string;
};

export type ChartViewPatch = {
  symbol?: string;
  timeframe?: string;
  source?: string;
  mode?: string;
};

export type ChartViewResponse = {
  session: string;
  chart: ChartViewState;
};

export type WatchlistState = {
  watchlist_symbols: string[];
};

export type ScheduledJobRow = {
  id: string;
  type: string;
  cron?: string | null;
  schedule?: string | null;
  spec?: Record<string, unknown>;
  prompt_preview: string;
  owner_session: string;
  next_run?: string | null;
  status: string;
  last_run?: string | null;
  run_count: number;
  max_runs?: number | null;
  created_at?: string;
  created_by?: string;
  last_result_preview?: string | null;
};

export type WatchlistPatch = {
  symbols?: string[];
  add?: string;
  remove?: string;
};

export type WatchlistResponse = {
  session: string;
  watchlist: WatchlistState;
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

export type AutoStartedEnvelope = {
  type: "auto_started";
  readonly: boolean;
  iterations_total: number;
  iterations_remaining: number;
  iterations_used: number;
  elapsed_seconds: number;
};

export type AutoProgressEnvelope = {
  type: "auto_progress";
  readonly: boolean;
  iterations_total: number;
  iterations_remaining: number;
  iterations_used: number;
  elapsed_seconds: number;
};

export type AutoStoppedEnvelope = {
  type: "auto_stopped";
  readonly: boolean;
  iterations_total: number;
  iterations_remaining: number;
  iterations_used: number;
  elapsed_seconds: number;
  reason: string;
};

export type SignalEnvelope = {
  type: "signal";
  signal: Record<string, unknown>;
};

export type ChartBarEnvelope = {
  type: "chart_bar";
  symbol: string;
  tf: string;
  bar: CandleBar;
};

export type ChartViewEnvelope = ChartViewState & {
  type: "chart_view";
};

export type WatchlistEnvelope = WatchlistState & {
  type: "watchlist";
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
  | AutoStartedEnvelope
  | AutoProgressEnvelope
  | AutoStoppedEnvelope
  | SignalEnvelope
  | ChartBarEnvelope
  | ChartViewEnvelope
  | WatchlistEnvelope
  | NatsEventEnvelope
  | ScheduledJobEnvelope;

export function isServerEnvelope(value: unknown): value is ServerEnvelope {
  if (!value || typeof value !== "object") {
    return false;
  }
  const envelope = value as { type?: unknown };
  return typeof envelope.type === "string" && envelope.type.length > 0;
}
