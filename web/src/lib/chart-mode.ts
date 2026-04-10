export const CHART_MODE_STORAGE_PREFIX = "kai.chart.mode";

export const CHART_MODES = ["full", "half", "mini", "hide"] as const;

export type ChartMode = (typeof CHART_MODES)[number];

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

export function resolveChartModeCommand(
  command: string,
  args: string,
): ChartMode | null {
  if (command.trim().toLowerCase() !== "/chart") {
    return null;
  }
  const normalizedArgs = args.trim().toLowerCase();
  if (!normalizedArgs) {
    return null;
  }
  const parts = normalizedArgs.split(/\s+/).filter(Boolean);
  if (parts[0] === "mode") {
    return parts.length === 2 ? parseChartModeToken(parts[1]) : null;
  }
  return parts.length === 1 ? parseChartModeToken(parts[0]) : null;
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
