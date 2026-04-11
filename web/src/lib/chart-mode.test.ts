import {
  CHART_MODES,
  CHART_TIMEFRAMES,
  cycleChartMode,
  normalizeChartMode,
  parseChartModeToken,
  parseChartTimeframeToken,
  resolveChartCommandInput,
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

  it("only parses known timeframe tokens", () => {
    for (const timeframe of CHART_TIMEFRAMES) {
      expect(parseChartTimeframeToken(timeframe)).toBe(timeframe);
    }
    expect(parseChartTimeframeToken("2h")).toBeNull();
    expect(parseChartTimeframeToken("BTC")).toBeNull();
  });

  it("detects chart slash commands for layout, symbol, and timeframe changes", () => {
    expect(resolveChartCommandInput("/chart", "full")).toEqual({ mode: "full" });
    expect(resolveChartCommandInput("/chart", "mode mini")).toEqual({ mode: "mini" });
    expect(resolveChartCommandInput("/chart", "hide")).toEqual({ mode: "hide" });
    expect(resolveChartCommandInput("/chart", "SOL")).toEqual({ symbol: "SOL" });
    expect(resolveChartCommandInput("/chart", "4h")).toEqual({ timeframe: "4h" });
    expect(resolveChartCommandInput("/chart", "BTC 1h")).toEqual({
      symbol: "BTC",
      timeframe: "1h",
    });
    expect(resolveChartCommandInput("/chart", "1d ETH")).toEqual({
      symbol: "ETH",
      timeframe: "1d",
    });
    expect(resolveChartCommandInput("/chart", "ETH SOL")).toBeNull();
    expect(resolveChartCommandInput("/status", "")).toBeNull();
  });

  it("cycles through the supported modes in a stable order", () => {
    let current: ChartMode = CHART_MODES[0];
    for (const next of CHART_MODES.slice(1).concat(CHART_MODES[0])) {
      current = cycleChartMode(current);
      expect(current).toBe(next);
    }
  });
});
