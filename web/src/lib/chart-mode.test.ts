import {
  CHART_MODES,
  cycleChartMode,
  normalizeChartMode,
  parseChartModeToken,
  resolveChartModeCommand,
  type ChartMode,
} from "$lib/chart-mode";

describe("chart mode helpers", () => {
  it("normalizes direct and daemon chart mode values", () => {
    expect(normalizeChartMode("full")).toBe("full");
    expect(normalizeChartMode("dashboard")).toBe("full");
    expect(normalizeChartMode("chat")).toBe("mini");
    expect(normalizeChartMode("focus")).toBe("hide");
    expect(normalizeChartMode("unknown")).toBe("full");
  });

  it("only parses known mode tokens", () => {
    expect(parseChartModeToken("mini")).toBe("mini");
    expect(parseChartModeToken("hidden")).toBe("hide");
    expect(parseChartModeToken("BTC")).toBeNull();
    expect(parseChartModeToken("")).toBeNull();
  });

  it("detects chart layout slash commands without catching symbol changes", () => {
    expect(resolveChartModeCommand("/chart", "full")).toBe("full");
    expect(resolveChartModeCommand("/chart", "mode mini")).toBe("mini");
    expect(resolveChartModeCommand("/chart", "hide")).toBe("hide");
    expect(resolveChartModeCommand("/chart", "BTC 1h")).toBeNull();
    expect(resolveChartModeCommand("/status", "")).toBeNull();
  });

  it("cycles through the supported modes in a stable order", () => {
    let current: ChartMode = CHART_MODES[0];
    for (const next of CHART_MODES.slice(1).concat(CHART_MODES[0])) {
      current = cycleChartMode(current);
      expect(current).toBe(next);
    }
  });
});
