import {
  applyChartBar,
  chartBarMatchesSubscription,
  chartSubscriptionActions,
  makeChartStreamKey,
  normalizeCandleBar,
} from "$lib/chart-stream";
import type { CandleBar } from "$lib/daemon/types";

describe("chart stream helpers", () => {
  it("builds unsubscribe-then-subscribe actions when the chart stream changes", () => {
    const previous = makeChartStreamKey("BTC", "1m");
    const next = makeChartStreamKey("DOGE", "1m");

    expect(chartSubscriptionActions(previous, next)).toEqual([
      { type: "unsubscribe", key: previous },
      { type: "subscribe", key: next },
    ]);
  });

  it("does not resubscribe when the stream key is unchanged", () => {
    expect(
      chartSubscriptionActions(
        makeChartStreamKey("BTC", "1m"),
        makeChartStreamKey("BTC", "1m"),
      ),
    ).toEqual([]);
  });

  it("drops stale chart bars from the old symbol or timeframe", () => {
    const active = makeChartStreamKey("ETH", "5m");

    expect(chartBarMatchesSubscription({ symbol: "BTC", tf: "5m" }, active)).toBe(false);
    expect(chartBarMatchesSubscription({ symbol: "ETH", tf: "1m" }, active)).toBe(false);
    expect(chartBarMatchesSubscription({ symbol: "ETH", tf: "5m" }, active)).toBe(true);
  });

  it("normalizes compact daemon bars into candle bars", () => {
    expect(
      normalizeCandleBar({
        ts: 1,
        o: "10",
        h: 12,
        l: 9,
        c: "11",
        v: 42,
      }),
    ).toEqual({
      ts: 1,
      open: 10,
      high: 12,
      low: 9,
      close: 11,
      volume: 42,
    });
  });

  it("reassigns, orders, replaces, and limits live bars", () => {
    const bars: CandleBar[] = [
      { ts: 2, open: 20, high: 21, low: 19, close: 20.5 },
      { ts: 1, open: 10, high: 11, low: 9, close: 10.5 },
    ];

    const replaced = applyChartBar(
      bars,
      { ts: 2, open: 21, high: 22, low: 20, close: 21.5 },
      2,
    );
    const appended = applyChartBar(
      replaced ?? [],
      { ts: 3, open: 30, high: 31, low: 29, close: 30.5 },
      2,
    );

    expect(replaced).not.toBe(bars);
    expect(bars[0]?.open).toBe(20);
    expect(appended).toEqual([
      { ts: 2, open: 21, high: 22, low: 20, close: 21.5 },
      { ts: 3, open: 30, high: 31, low: 29, close: 30.5 },
    ]);
  });
});
