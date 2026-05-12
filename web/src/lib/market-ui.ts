import type { ChartViewPatch, WatchlistQuote } from "$lib/daemon/types";

export type SymbolSuggestion = {
  symbol: string;
  label: string;
  source: "active" | "watchlist" | "signal" | "known";
};

export type SignalMacdSummary = {
  value?: number;
  signal?: number;
  histogram?: number;
  state?: string;
};

export type SignalAlert = {
  id: string;
  timestamp: string;
  localTime: string;
  relativeTime: string;
  symbol: string;
  side: string;
  score?: number;
  price?: number;
  timeframe?: string;
  rsi?: number;
  macd?: SignalMacdSummary;
  source?: string;
  reason?: string;
  raw: Record<string, unknown>;
};

export type SignalSideFilter = "all" | "long" | "short" | "neutral";

export type SignalFilters = {
  symbolQuery: string;
  side: SignalSideFilter;
  actionableOnly: boolean;
  minScore?: number;
};

export type WatchlistSortMode =
  | "manual"
  | "change"
  | "price"
  | "volume"
  | "signals";

const KNOWN_SYMBOLS = [
  "BTC",
  "ETH",
  "SOL",
  "XRP",
  "DOGE",
  "ADA",
  "AVAX",
  "LINK",
  "MATIC",
  "BNB",
];

function normalizeSymbol(value: unknown): string {
  return String(value ?? "").trim().toUpperCase();
}

export function normalizeMarketSymbol(value: string): string {
  return normalizeSymbol(value);
}

export function shouldRefitPriceScale(prevMarketKey: string, nextMarketKey: string): boolean {
  return prevMarketKey.split(":")[0] !== nextMarketKey.split(":")[0];
}

function numberFrom(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function stringFrom(value: unknown): string | undefined {
  const text = String(value ?? "").trim();
  return text || undefined;
}

function firstString(payload: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = stringFrom(payload[key]);
    if (value) {
      return value;
    }
  }
  return undefined;
}

function firstNumber(payload: Record<string, unknown>, keys: string[]): number | undefined {
  for (const key of keys) {
    const value = numberFrom(payload[key]);
    if (typeof value === "number") {
      return value;
    }
  }
  return undefined;
}

function nestedRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function normalizeTimestamp(payload: Record<string, unknown>): string {
  const raw =
    firstString(payload, ["timestamp", "ts", "time", "created_at", "createdAt"]) ??
    new Date().toISOString();
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? new Date().toISOString() : parsed.toISOString();
}

export function formatLocalTime(timestamp: string): string {
  const parsed = new Date(timestamp);
  if (Number.isNaN(parsed.getTime())) {
    return "--:--:--";
  }
  return parsed.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatChatTimestamp(
  timestamp: string | undefined,
  timeZone = "America/New_York",
): string | null {
  if (!timestamp) {
    return null;
  }
  const parsed = new Date(timestamp);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  return parsed.toLocaleTimeString("en-US", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
  });
}

export function formatRelativeTime(timestamp: string, now = Date.now()): string {
  const parsed = new Date(timestamp).getTime();
  if (Number.isNaN(parsed)) {
    return "";
  }
  const deltaSeconds = Math.max(0, Math.round((now - parsed) / 1000));
  if (deltaSeconds < 60) {
    return `${deltaSeconds}s ago`;
  }
  const deltaMinutes = Math.round(deltaSeconds / 60);
  if (deltaMinutes < 60) {
    return `${deltaMinutes}m ago`;
  }
  const deltaHours = Math.round(deltaMinutes / 60);
  return `${deltaHours}h ago`;
}

export function formatPriceCompact(value?: number): string {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "--";
  }
  if (Math.abs(value) >= 1000) {
    return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  }
  if (Math.abs(value) >= 1) {
    return `$${value.toLocaleString(undefined, {
      maximumFractionDigits: 4,
      minimumFractionDigits: 2,
    })}`;
  }
  return `$${value.toPrecision(4)}`;
}

export function normalizeSignalAlert(
  payload: Record<string, unknown>,
  index = 0,
  now = Date.now(),
): SignalAlert {
  const indicators = nestedRecord(payload.indicators);
  const macdPayload = nestedRecord(payload.macd ?? indicators.macd);
  const timestamp = normalizeTimestamp(payload);
  const symbol = normalizeSymbol(payload.symbol ?? payload.market ?? payload.pair) || "?";
  const side =
    firstString(payload, ["side", "direction", "signal", "type", "signal_type"]) ?? "signal";
  const source = firstString(payload, ["source", "strategy", "strategy_name"]);
  const reason = firstString(payload, ["reason", "message", "summary", "description"]);
  const timeframe = firstString(payload, ["timeframe", "tf", "interval"]);
  return {
    id: firstString(payload, ["id", "signal_id"]) ?? `${timestamp}-${symbol}-${index}`,
    timestamp,
    localTime: formatLocalTime(timestamp),
    relativeTime: formatRelativeTime(timestamp, now),
    symbol,
    side,
    score: firstNumber(payload, ["score", "confidence", "probability"]),
    price: firstNumber(payload, ["price", "entry_price", "current_price"]),
    timeframe,
    rsi: firstNumber(payload, ["rsi", "rsi_14"]) ?? numberFrom(indicators.rsi),
    macd: {
      value: firstNumber(macdPayload, ["value", "macd"]),
      signal: firstNumber(macdPayload, ["signal"]),
      histogram: firstNumber(macdPayload, ["histogram", "hist"]),
      state: firstString(macdPayload, ["state", "trend", "direction"]),
    },
    source,
    reason,
    raw: payload,
  };
}

export function signalChartPatch(alert: SignalAlert): ChartViewPatch {
  return {
    symbol: alert.symbol && alert.symbol !== "?" ? alert.symbol : undefined,
    timeframe: alert.timeframe,
  };
}

export function isActionableSignal(alert: SignalAlert): boolean {
  const side = alert.side.toLowerCase();
  const hasDirection =
    side.includes("long") ||
    side.includes("buy") ||
    side.includes("short") ||
    side.includes("sell");
  const hasEnoughConfidence =
    typeof alert.score !== "number" || alert.score >= 0.5 || alert.score >= 50;
  return alert.symbol !== "?" && hasDirection && hasEnoughConfidence;
}

export function signalSideBucket(alert: SignalAlert): SignalSideFilter {
  const side = alert.side.toLowerCase();
  if (side.includes("long") || side.includes("buy")) {
    return "long";
  }
  if (side.includes("short") || side.includes("sell")) {
    return "short";
  }
  return "neutral";
}

export function filterSignalAlerts(
  alerts: SignalAlert[],
  filters: SignalFilters,
): SignalAlert[] {
  const query = filters.symbolQuery.trim().toUpperCase();
  return alerts.filter((alert) => {
    if (query && !alert.symbol.includes(query)) {
      return false;
    }
    if (filters.side !== "all" && signalSideBucket(alert) !== filters.side) {
      return false;
    }
    if (filters.actionableOnly && !isActionableSignal(alert)) {
      return false;
    }
    if (
      typeof filters.minScore === "number" &&
      typeof alert.score === "number" &&
      alert.score < filters.minScore
    ) {
      return false;
    }
    return true;
  });
}

export function signalCountsBySymbol(alerts: SignalAlert[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const alert of alerts) {
    if (alert.symbol === "?") {
      continue;
    }
    counts[alert.symbol] = (counts[alert.symbol] ?? 0) + 1;
  }
  return counts;
}

export function sortWatchlistQuotes(
  quotes: WatchlistQuote[],
  sortMode: WatchlistSortMode,
  signalCounts: Record<string, number> = {},
): WatchlistQuote[] {
  const sorted = [...quotes];
  const numeric = (value: number | undefined) =>
    typeof value === "number" && Number.isFinite(value) ? value : -Infinity;
  if (sortMode === "change") {
    sorted.sort(
      (left, right) =>
        numeric(right.price_change_24h_pct) - numeric(left.price_change_24h_pct),
    );
  } else if (sortMode === "price") {
    sorted.sort((left, right) => numeric(right.price) - numeric(left.price));
  } else if (sortMode === "volume") {
    sorted.sort((left, right) => numeric(right.volume_24h) - numeric(left.volume_24h));
  } else if (sortMode === "signals") {
    sorted.sort(
      (left, right) =>
        (signalCounts[right.symbol] ?? 0) - (signalCounts[left.symbol] ?? 0),
    );
  }
  return sorted;
}

export function filterWatchlistQuotes(
  quotes: WatchlistQuote[],
  query: string,
): WatchlistQuote[] {
  const normalized = query.trim().toUpperCase();
  if (!normalized) {
    return quotes;
  }
  return quotes.filter((quote) => quote.symbol.includes(normalized));
}

export function buildSymbolSuggestions(options: {
  activeSymbol: string;
  watchlist: string[];
  quotes: WatchlistQuote[];
  signals: SignalAlert[];
  query: string;
  limit?: number;
}): SymbolSuggestion[] {
  const query = options.query.trim().toUpperCase();
  const seen = new Set<string>();
  const suggestions: SymbolSuggestion[] = [];
  const add = (symbol: string, source: SymbolSuggestion["source"]) => {
    const normalized = normalizeSymbol(symbol);
    if (!normalized || seen.has(normalized)) {
      return;
    }
    seen.add(normalized);
    suggestions.push({ symbol: normalized, label: normalized, source });
  };
  add(options.activeSymbol, "active");
  for (const quote of options.quotes) {
    add(quote.symbol, "watchlist");
  }
  for (const symbol of options.watchlist) {
    add(symbol, "watchlist");
  }
  for (const signal of options.signals) {
    add(signal.symbol, "signal");
  }
  for (const symbol of KNOWN_SYMBOLS) {
    add(symbol, "known");
  }
  const filtered = query
    ? suggestions.filter((item) => item.symbol.includes(query))
    : suggestions;
  return filtered.slice(0, options.limit ?? 8);
}
