import {
  buildSymbolSuggestions,
  filterSignalAlerts,
  filterWatchlistQuotes,
  formatRelativeTime,
  isActionableSignal,
  normalizeSignalAlert,
  shouldRefitPriceScale,
  signalChartPatch,
  signalCountsBySymbol,
  sortWatchlistQuotes,
} from "$lib/market-ui";

describe("market UI helpers", () => {
  it("normalizes signal payloads with price and indicators", () => {
    const alert = normalizeSignalAlert(
      {
        timestamp: "2026-04-23T21:00:00Z",
        symbol: "btc",
        side: "long",
        score: "0.82",
        price: "78240.50",
        timeframe: "15m",
        indicators: { rsi: 61.4 },
        macd: { state: "bullish", histogram: 12.5 },
        strategy: "breakout",
        reason: "confirmation",
      },
      0,
      Date.parse("2026-04-23T21:01:30Z"),
    );

    expect(alert.symbol).toBe("BTC");
    expect(alert.side).toBe("long");
    expect(alert.score).toBe(0.82);
    expect(alert.price).toBe(78240.5);
    expect(alert.rsi).toBe(61.4);
    expect(alert.macd?.state).toBe("bullish");
    expect(alert.relativeTime).toBe("2m ago");
    expect(signalChartPatch(alert)).toEqual({ symbol: "BTC", timeframe: "15m" });
  });

  it("uses signal_type as the final side fallback for signal alerts", () => {
    const alert = normalizeSignalAlert(
      { symbol: "FLORKETH", signal_type: "BUY", price: 0.000002207 },
      0,
    );

    expect(alert.side).toBe("BUY");
    expect(isActionableSignal(alert)).toBe(true);
  });

  it("prefers canonical type over signal_type when both are present", () => {
    const alert = normalizeSignalAlert({
      symbol: "FLORKETH",
      type: "SELL",
      signal_type: "BUY",
    });

    expect(alert.side).toBe("SELL");
  });

  it("builds local symbol suggestions from chart, watchlist, signals, and known symbols", () => {
    const suggestions = buildSymbolSuggestions({
      activeSymbol: "BTC",
      watchlist: ["ETH", "SOL"],
      quotes: [{ symbol: "DOGE" }],
      signals: [normalizeSignalAlert({ symbol: "AVAX" })],
      query: "o",
    });

    expect(suggestions.map((item) => item.symbol)).toEqual(["DOGE", "SOL"]);
  });

  it("formats relative time using compact units", () => {
    expect(
      formatRelativeTime(
        "2026-04-23T21:00:00Z",
        Date.parse("2026-04-23T21:00:44Z"),
      ),
    ).toBe("44s ago");
  });

  it("refits the chart price scale only when the symbol changes", () => {
    expect(shouldRefitPriceScale("", "BTC:1h:live")).toBe(true);
    expect(shouldRefitPriceScale("BTC:1h:live", "DOGE:1h:live")).toBe(true);
    expect(shouldRefitPriceScale("BTC:1h:live", "BTC:4h:live")).toBe(false);
    expect(shouldRefitPriceScale("BTC:1h:live", "BTC:1h:paper")).toBe(false);
  });

  it("filters actionable directional signals", () => {
    const alerts = [
      normalizeSignalAlert({ symbol: "BTC", side: "long", score: 0.8 }),
      normalizeSignalAlert({ symbol: "ETH", side: "short", score: 0.2 }),
      normalizeSignalAlert({ symbol: "SOL", side: "watch" }),
    ];

    expect(isActionableSignal(alerts[0])).toBe(true);
    expect(
      filterSignalAlerts(alerts, {
        symbolQuery: "",
        side: "long",
        actionableOnly: true,
      }).map((alert) => alert.symbol),
    ).toEqual(["BTC"]);
  });

  it("filters and sorts watchlist quotes for scanning", () => {
    const quotes = [
      { symbol: "BTC", price: 10, price_change_24h_pct: 1, volume_24h: 20 },
      { symbol: "ETH", price: 20, price_change_24h_pct: 3, volume_24h: 10 },
      { symbol: "SOL", price: 5, price_change_24h_pct: -2, volume_24h: 30 },
    ];
    const signalCounts = signalCountsBySymbol([
      normalizeSignalAlert({ symbol: "SOL" }),
      normalizeSignalAlert({ symbol: "SOL" }),
      normalizeSignalAlert({ symbol: "BTC" }),
    ]);

    expect(filterWatchlistQuotes(quotes, "t").map((quote) => quote.symbol)).toEqual([
      "BTC",
      "ETH",
    ]);
    expect(sortWatchlistQuotes(quotes, "signals", signalCounts)[0].symbol).toBe("SOL");
    expect(sortWatchlistQuotes(quotes, "change")[0].symbol).toBe("ETH");
  });
});
