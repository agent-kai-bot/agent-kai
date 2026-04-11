export const CHART_MODE_STORAGE_PREFIX = "kai.chart.mode";

export const CHART_MODES = ["full", "half", "mini", "hide"] as const;
export const CHART_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d", "1w"] as const;

export type ChartMode = (typeof CHART_MODES)[number];
export type ChartTimeframe = (typeof CHART_TIMEFRAMES)[number];
export type ChartCommandInput =
  | { mode: ChartMode; symbol?: undefined; timeframe?: undefined }
  | { mode?: undefined; symbol?: string; timeframe?: ChartTimeframe };

const CHART_MODE_LABELS: Record<ChartMode, string> = {
  full: "Full",
  half: "Half",
  mini: "Mini",
  hide: "Hidden",
};

const DIRECT_MODE_ALIASES: Record<string, ChartMode> = {
  full: "full",
  default: "full",
  half: "half",
  mini: "mini",
  minimal: "mini",
  hide: "hide",
  hidden: "hide",
  off: "hide",
};

const DAEMON_MODE_ALIASES: Record<string, ChartMode> = {
  dashboard: "full",
  inspect: "full",
  zen: "half",
  chat: "mini",
  focus: "hide",
};

export function chartModeLabel(mode: ChartMode): string {
  return CHART_MODE_LABELS[mode];
}

export function cycleChartMode(current: ChartMode): ChartMode {
  const currentIndex = CHART_MODES.indexOf(current);
  return CHART_MODES[(currentIndex + 1) % CHART_MODES.length];
}

export function parseChartModeToken(value: string | null | undefined): ChartMode | null {
  if (!value) {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  return DIRECT_MODE_ALIASES[normalized] ?? DAEMON_MODE_ALIASES[normalized] ?? null;
}

export function normalizeChartMode(value: string | null | undefined): ChartMode {
  return parseChartModeToken(value) ?? "full";
}

export function parseChartTimeframeToken(
  value: string | null | undefined,
): ChartTimeframe | null {
  if (!value) {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  return (
    CHART_TIMEFRAMES.find((timeframe) => timeframe === normalized) ?? null
  );
}

export function resolveChartCommandInput(
  command: string,
  args: string,
): ChartCommandInput | null {
  if (command.trim().toLowerCase() !== "/chart") {
    return null;
  }
  const normalizedArgs = args.trim();
  if (!normalizedArgs) {
    return null;
  }
  const parts = normalizedArgs.split(/\s+/).filter(Boolean);
  if (parts[0].toLowerCase() === "mode") {
    const mode = parts.length === 2 ? parseChartModeToken(parts[1]) : null;
    return mode ? { mode } : null;
  }
  if (parts.length === 1) {
    const mode = parseChartModeToken(parts[0]);
    if (mode) {
      return { mode };
    }
    const timeframe = parseChartTimeframeToken(parts[0]);
    if (timeframe) {
      return { timeframe };
    }
    return { symbol: parts[0] };
  }
  if (parts.length !== 2) {
    return null;
  }

  const [first, second] = parts;
  if (parseChartModeToken(first) || parseChartModeToken(second)) {
    return null;
  }

  const firstTimeframe = parseChartTimeframeToken(first);
  const secondTimeframe = parseChartTimeframeToken(second);
  if (firstTimeframe && secondTimeframe) {
    return null;
  }
  if (firstTimeframe) {
    return { symbol: second, timeframe: firstTimeframe };
  }
  if (secondTimeframe) {
    return { symbol: first, timeframe: secondTimeframe };
  }
  return null;
}

function chartModeStorageKey(sessionName: string): string {
  return `${CHART_MODE_STORAGE_PREFIX}.${sessionName.trim().toLowerCase()}`;
}

export function readStoredChartMode(sessionName: string): ChartMode | null {
  if (typeof window === "undefined") {
    return null;
  }
  const normalizedSession = sessionName.trim();
  if (!normalizedSession) {
    return null;
  }
  try {
    const storedValue = window.localStorage.getItem(chartModeStorageKey(normalizedSession));
    return storedValue ? normalizeChartMode(storedValue) : null;
  } catch {
    return null;
  }
}

export function writeStoredChartMode(sessionName: string, mode: ChartMode): void {
  if (typeof window === "undefined") {
    return;
  }
  const normalizedSession = sessionName.trim();
  if (!normalizedSession) {
    return;
  }
  try {
    window.localStorage.setItem(chartModeStorageKey(normalizedSession), mode);
  } catch {
    // Ignore storage failures so layout changes never block the UI.
  }
}
