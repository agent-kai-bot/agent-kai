import type { CandleBar, ChartBarEnvelope } from "$lib/daemon/types";

export type ChartStreamKey = {
  symbol: string;
  timeframe: string;
};

export type ChartSubscriptionAction = {
  type: "subscribe" | "unsubscribe";
  key: ChartStreamKey;
};

type RawCandleBar = Record<string, unknown>;

export function makeChartStreamKey(symbol: string, timeframe: string): ChartStreamKey {
  return { symbol, timeframe };
}

export function chartStreamKeyLabel(key: ChartStreamKey): string {
  return `${key.symbol} ${key.timeframe}`;
}

export function sameChartStreamKey(
  left: ChartStreamKey | null,
  right: ChartStreamKey | null,
): boolean {
  return left?.symbol === right?.symbol && left?.timeframe === right?.timeframe;
}

export function chartSubscriptionActions(
  previous: ChartStreamKey | null,
  next: ChartStreamKey | null,
): ChartSubscriptionAction[] {
  if (sameChartStreamKey(previous, next)) {
    return [];
  }

  const actions: ChartSubscriptionAction[] = [];
  if (previous) {
    actions.push({ type: "unsubscribe", key: previous });
  }
  if (next) {
    actions.push({ type: "subscribe", key: next });
  }
  return actions;
}

export function chartBarMatchesSubscription(
  envelope: Pick<ChartBarEnvelope, "symbol" | "tf">,
  subscription: ChartStreamKey | null,
): boolean {
  return Boolean(
    subscription &&
      envelope.symbol === subscription.symbol &&
      envelope.tf === subscription.timeframe,
  );
}

function asRecord(value: unknown): RawCandleBar | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as RawCandleBar;
}

function firstNumber(payload: RawCandleBar, keys: string[]): number | undefined {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }
  return undefined;
}

function firstTimestamp(payload: RawCandleBar): CandleBar["ts"] | undefined {
  for (const key of ["ts", "time", "timestamp"]) {
    const value = payload[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return undefined;
}

function timestampSortValue(value: CandleBar["ts"]): number {
  if (typeof value === "number") {
    return value;
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function normalizeCandleBar(rawBar: unknown): CandleBar | null {
  const payload = asRecord(rawBar);
  if (!payload) {
    return null;
  }

  const ts = firstTimestamp(payload);
  const open = firstNumber(payload, ["open", "o"]);
  const high = firstNumber(payload, ["high", "h"]);
  const low = firstNumber(payload, ["low", "l"]);
  const close = firstNumber(payload, ["close", "c"]);
  const volume = firstNumber(payload, ["volume", "v"]);

  if (
    ts === undefined ||
    open === undefined ||
    high === undefined ||
    low === undefined ||
    close === undefined
  ) {
    return null;
  }

  return volume === undefined
    ? { ts, open, high, low, close }
    : { ts, open, high, low, close, volume };
}

export function applyChartBar(
  bars: CandleBar[],
  rawBar: unknown,
  limit = 300,
): CandleBar[] | null {
  const bar = normalizeCandleBar(rawBar);
  if (!bar) {
    return null;
  }

  const nextBars = [...bars];
  const existingIndex = nextBars.findIndex((item) => String(item.ts) === String(bar.ts));
  if (existingIndex === -1) {
    nextBars.push(bar);
  } else {
    nextBars[existingIndex] = bar;
  }

  nextBars.sort((left, right) => timestampSortValue(left.ts) - timestampSortValue(right.ts));
  return limit <= 0 ? [] : nextBars.slice(-limit);
}
