<script lang="ts">
  import { onMount } from "svelte";
  import {
    CandlestickSeries,
    ColorType,
    HistogramSeries,
    createChart,
    type CandlestickData,
    type HistogramData,
    type IChartApi,
    type ISeriesApi,
    type Time,
    type UTCTimestamp,
  } from "lightweight-charts";

  import type { CandleBar } from "$lib/daemon/types";

  import Panel from "$lib/components/Panel.svelte";

  let {
    bars,
    symbol,
    timeframe,
    source,
    status = "",
  }: {
    bars: CandleBar[];
    symbol: string;
    timeframe: string;
    source: string;
    status?: string;
  } = $props();

  let container: HTMLDivElement;
  let chart: IChartApi | null = null;
  let candleSeries: ISeriesApi<"Candlestick"> | null = null;
  let volumeSeries: ISeriesApi<"Histogram"> | null = null;
  let hasFitted = false;

  function toUtcTimestamp(value: CandleBar["ts"]): UTCTimestamp {
    if (typeof value === "number") {
      return Math.floor(value) as UTCTimestamp;
    }
    return Math.floor(new Date(value).getTime() / 1000) as UTCTimestamp;
  }

  function applyBars(): void {
    if (!candleSeries || !volumeSeries) {
      return;
    }
    const candleData: CandlestickData<Time>[] = bars.map((bar) => ({
      time: toUtcTimestamp(bar.ts),
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));
    const volumeData: HistogramData<Time>[] = bars.map((bar) => ({
      time: toUtcTimestamp(bar.ts),
      value: bar.volume ?? 0,
      color: bar.close >= bar.open ? "#4dd3a8" : "#ff8f8f",
    }));

    candleSeries.setData(candleData);
    volumeSeries.setData(volumeData);
    if (!hasFitted && candleData.length) {
      chart?.timeScale().fitContent();
      hasFitted = true;
    }
  }

  onMount(() => {
    chart = createChart(container, {
      width: container.clientWidth,
      height: 360,
      layout: {
        background: { type: ColorType.Solid, color: "#06131f" },
        textColor: "#f2f6fb",
      },
      grid: {
        vertLines: { color: "rgba(145, 181, 221, 0.08)" },
        horzLines: { color: "rgba(145, 181, 221, 0.08)" },
      },
      rightPriceScale: {
        borderColor: "rgba(145, 181, 221, 0.14)",
      },
      timeScale: {
        borderColor: "rgba(145, 181, 221, 0.14)",
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        vertLine: { color: "rgba(77, 211, 168, 0.25)" },
        horzLine: { color: "rgba(243, 196, 106, 0.22)" },
      },
    });

    candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#4dd3a8",
      downColor: "#ff8f8f",
      wickUpColor: "#4dd3a8",
      wickDownColor: "#ff8f8f",
      borderVisible: false,
    });

    volumeSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: "",
      priceFormat: { type: "volume" },
    });

    volumeSeries.priceScale().applyOptions({
      scaleMargins: {
        top: 0.78,
        bottom: 0,
      },
    });

    const observer = new ResizeObserver(() => {
      chart?.applyOptions({
        width: container.clientWidth,
        height: Math.max(container.clientHeight, 360),
      });
    });
    observer.observe(container);
    applyBars();

    return () => {
      observer.disconnect();
      chart?.remove();
      chart = null;
      candleSeries = null;
      volumeSeries = null;
    };
  });

  $effect(() => {
    bars;
    applyBars();
  });
</script>

<Panel eyebrow="Visualization" title="Chart" subtitle={`${symbol} ${timeframe} · ${source}`}>
  <div class="chart-shell">
    <div bind:this={container} class="chart-canvas"></div>
    <div class="chart-meta">
      <strong>{bars.length} bars</strong>
      <span>{status || "live snapshot"}</span>
    </div>
  </div>
</Panel>

<style>
  .chart-shell {
    display: grid;
    gap: 0.8rem;
  }

  .chart-canvas {
    min-height: 22rem;
    border: 1px solid rgba(145, 181, 221, 0.1);
    border-radius: 1rem;
    overflow: hidden;
  }

  .chart-meta {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    color: var(--muted);
  }

  .chart-meta strong {
    color: var(--text);
  }
</style>
