<script lang="ts">
  import Panel from "$lib/components/Panel.svelte";
  import type { ChartMode } from "$lib/chart-mode";

  let {
    activeSession,
    currentStatus,
    queueDepth,
    selectedAgentLabel,
    modelStatus,
    snapshotSummary,
    chartSymbol,
    chartTimeframe,
    chartSource,
    chartMode,
    chartUpdateLabel,
    chartPriceLabel,
    chartChangeLabel,
    streamLatencyLabel,
    streamThroughputLabel,
    positionsCount,
    watchlistCount,
    signalCount,
    schedulerEventCount,
    natsEventCount,
    attachError = "",
    mobileCollapsible = false,
    initiallyOpen = true,
  }: {
    activeSession: string;
    currentStatus: string;
    queueDepth: number;
    selectedAgentLabel: string;
    modelStatus: string;
    snapshotSummary: string;
    chartSymbol: string;
    chartTimeframe: string;
    chartSource: string;
    chartMode: ChartMode;
    chartUpdateLabel: string;
    chartPriceLabel: string;
    chartChangeLabel: string;
    streamLatencyLabel: string;
    streamThroughputLabel: string;
    positionsCount: number;
    watchlistCount: number;
    signalCount: number;
    schedulerEventCount: number;
    natsEventCount: number;
    attachError?: string;
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
  } = $props();

  type OverviewItem = {
    label: string;
    value: string | number;
    tone?: "default" | "positive" | "negative" | "warning";
  };

  function changeTone(value: string): OverviewItem["tone"] {
    if (value.startsWith("+")) {
      return "positive";
    }
    if (value.startsWith("-")) {
      return "negative";
    }
    return "default";
  }

  function sessionItems(): OverviewItem[] {
    return [
      { label: "Session", value: activeSession || "not attached" },
      { label: "Status", value: currentStatus || "unknown" },
      { label: "Queue", value: queueDepth },
    ];
  }

  function marketItems(): OverviewItem[] {
    return [
      { label: "Symbol", value: chartSymbol },
      { label: "Timeframe", value: chartTimeframe },
      { label: "Source", value: chartSource },
      { label: "Mode", value: chartMode },
      { label: "Price", value: chartPriceLabel },
      { label: "24h", value: chartChangeLabel, tone: changeTone(chartChangeLabel) },
    ];
  }

  function runtimeItems(): OverviewItem[] {
    return [
      { label: "Model", value: selectedAgentLabel },
      { label: "Model status", value: modelStatus },
      { label: "Stream", value: streamLatencyLabel },
      { label: "Throughput", value: streamThroughputLabel },
      { label: "Chart", value: chartUpdateLabel, tone: chartUpdateLabel === "Ready" ? "default" : "warning" },
    ];
  }

  function activityItems(): OverviewItem[] {
    return [
      { label: "Positions", value: positionsCount },
      { label: "Watchlist", value: watchlistCount },
      { label: "Signals", value: signalCount },
      { label: "NATS", value: natsEventCount },
      { label: "Scheduled", value: schedulerEventCount },
    ];
  }
</script>

<Panel
  eyebrow="Status"
  title="Overview"
  subtitle={snapshotSummary || `${chartSymbol} ${chartTimeframe}`}
  initiallyOpen={initiallyOpen}
  mobileCollapsible={mobileCollapsible}
>
  <div class="overview-panel">
    {#if attachError}
      <p class="overview-error">{attachError}</p>
    {/if}

    <section aria-label="Session overview">
      <h3>Session</h3>
      <dl>
        {#each sessionItems() as item}
          <div>
            <dt>{item.label}</dt>
            <dd class={item.tone ?? "default"}>{item.value}</dd>
          </div>
        {/each}
      </dl>
    </section>

    <section aria-label="Market overview">
      <h3>Market</h3>
      <dl>
        {#each marketItems() as item}
          <div>
            <dt>{item.label}</dt>
            <dd class={item.tone ?? "default"}>{item.value}</dd>
          </div>
        {/each}
      </dl>
    </section>

    <section aria-label="Runtime overview">
      <h3>Runtime</h3>
      <dl>
        {#each runtimeItems() as item}
          <div>
            <dt>{item.label}</dt>
            <dd class={item.tone ?? "default"}>{item.value}</dd>
          </div>
        {/each}
      </dl>
    </section>

    <section aria-label="Activity overview">
      <h3>Activity</h3>
      <dl class="compact">
        {#each activityItems() as item}
          <div>
            <dt>{item.label}</dt>
            <dd>{item.value}</dd>
          </div>
        {/each}
      </dl>
    </section>
  </div>
</Panel>

<style>
  .overview-panel {
    display: grid;
    gap: 0.8rem;
  }

  .overview-panel section {
    display: grid;
    gap: 0.45rem;
  }

  .overview-panel h3 {
    margin: 0;
    color: var(--accent-strong);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .overview-panel dl {
    display: grid;
    gap: 0.35rem;
    margin: 0;
  }

  .overview-panel dl.compact {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .overview-panel dl div {
    display: grid;
    gap: 0.15rem;
    min-width: 0;
    border: 1px solid rgba(145, 181, 221, 0.08);
    border-radius: 0.65rem;
    background: rgba(7, 19, 31, 0.52);
    padding: 0.45rem 0.55rem;
  }

  .overview-panel dt {
    color: var(--muted);
    font-size: 0.72rem;
  }

  .overview-panel dd {
    margin: 0;
    min-width: 0;
    overflow-wrap: anywhere;
    color: var(--text);
    font-size: 0.84rem;
    font-weight: 700;
    line-height: 1.35;
  }

  .overview-panel dd.positive {
    color: var(--accent);
  }

  .overview-panel dd.negative,
  .overview-error {
    color: #ff8f8f;
  }

  .overview-panel dd.warning {
    color: var(--accent-strong);
  }

  .overview-error {
    margin: 0;
    border: 1px solid rgba(255, 143, 143, 0.22);
    border-radius: 0.75rem;
    background: rgba(61, 16, 16, 0.32);
    padding: 0.6rem 0.7rem;
    line-height: 1.4;
  }
</style>
