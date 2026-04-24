<script lang="ts">
  import Panel from "$lib/components/Panel.svelte";
  import {
    filterSignalAlerts,
    formatPriceCompact,
    isActionableSignal,
    signalSideBucket,
    type SignalAlert,
    type SignalSideFilter,
  } from "$lib/market-ui";

  let {
    alerts,
    activeSymbol = "",
    selectedId = "",
    onSelect,
    mobileCollapsible = false,
    initiallyOpen = true,
  }: {
    alerts: SignalAlert[];
    activeSymbol?: string;
    selectedId?: string;
    onSelect?: (alert: SignalAlert) => void;
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
  } = $props();

  let symbolQuery = $state("");
  let sideFilter = $state<SignalSideFilter>("all");
  let actionableOnly = $state(false);

  function visibleAlerts(): SignalAlert[] {
    return filterSignalAlerts(alerts, {
      symbolQuery,
      side: sideFilter,
      actionableOnly,
    });
  }

  function formatScore(value?: number): string {
    if (typeof value !== "number" || Number.isNaN(value)) {
      return "--";
    }
    return value <= 1 ? value.toFixed(2) : `${value.toFixed(0)}%`;
  }

  function macdLabel(alert: SignalAlert): string {
    if (alert.macd?.state) {
      return alert.macd.state;
    }
    if (typeof alert.macd?.histogram === "number") {
      return alert.macd.histogram.toFixed(2);
    }
    if (typeof alert.macd?.value === "number") {
      return alert.macd.value.toFixed(2);
    }
    return "--";
  }
</script>

<Panel
  eyebrow="Signals"
  initiallyOpen={initiallyOpen}
  mobileCollapsible={mobileCollapsible}
  subtitle={`${visibleAlerts().length}/${alerts.length} visible`}
  title="Alerts"
>
  <div class="signal-filters">
    <label>
      <span>Symbol</span>
      <input bind:value={symbolQuery} placeholder={activeSymbol || "BTC"} type="search" />
    </label>
    <label>
      <span>Side</span>
      <select bind:value={sideFilter}>
        <option value="all">All</option>
        <option value="long">Long</option>
        <option value="short">Short</option>
        <option value="neutral">Neutral</option>
      </select>
    </label>
    <label class="toggle">
      <input bind:checked={actionableOnly} type="checkbox" />
      <span>Actionable</span>
    </label>
  </div>

  {#if visibleAlerts().length}
    <ul class="signals">
      {#each visibleAlerts() as alert (alert.id)}
        <li
          class:active={alert.id === selectedId}
          class:long={signalSideBucket(alert) === "long"}
          class:short={signalSideBucket(alert) === "short"}
        >
          <article>
            <div class="signal-head">
              <div>
                <strong>{alert.symbol} {alert.side.toUpperCase()}</strong>
                <span>{alert.localTime} · {alert.relativeTime}</span>
              </div>
              <button
                aria-label={`Open ${alert.symbol} chart from signal`}
                disabled={!isActionableSignal(alert)}
                onclick={() => onSelect?.(alert)}
                type="button"
              >
                Chart
              </button>
            </div>
            <p>{alert.reason ?? "signal received"}</p>
            <div class="signal-metrics">
              <span>price {formatPriceCompact(alert.price)}</span>
              <span>score {formatScore(alert.score)}</span>
              <span>RSI {typeof alert.rsi === "number" ? alert.rsi.toFixed(1) : "--"}</span>
              <span>MACD {macdLabel(alert)}</span>
              {#if alert.timeframe}<span>{alert.timeframe}</span>{/if}
              {#if alert.source}<span>{alert.source}</span>{/if}
            </div>
          </article>
        </li>
      {/each}
    </ul>
  {:else if alerts.length}
    <p class="empty">No signals match the current filters.</p>
  {:else}
    <p class="empty">No alert envelopes yet.</p>
  {/if}
</Panel>

<style>
  .signal-filters {
    display: grid;
    gap: 0.55rem;
    grid-template-columns: minmax(0, 1fr) auto auto;
    margin-bottom: 0.75rem;
  }

  .signal-filters label {
    display: grid;
    gap: 0.3rem;
  }

  .signal-filters span {
    color: var(--muted);
    font-size: 0.76rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  .signal-filters input[type="search"],
  .signal-filters select {
    border: 1px solid rgba(145, 181, 221, 0.16);
    border-radius: 0.65rem;
    background: rgba(7, 19, 31, 0.86);
    color: inherit;
    font: inherit;
    padding: 0.6rem 0.7rem;
  }

  .signal-filters .toggle {
    align-content: end;
    display: flex;
    align-items: center;
    gap: 0.45rem;
    padding-bottom: 0.55rem;
  }

  .signals {
    display: grid;
    gap: 0.65rem;
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .signals article {
    border: 1px solid rgba(145, 181, 221, 0.1);
    border-radius: 0.9rem;
    background: rgba(7, 19, 31, 0.68);
    padding: 0.8rem;
  }

  .signals li.active article {
    border-color: rgba(77, 211, 168, 0.48);
    background: rgba(77, 211, 168, 0.1);
  }

  .signals li.long article {
    border-left: 3px solid #4dd3a8;
  }

  .signals li.short article {
    border-left: 3px solid #ff8f8f;
  }

  .signal-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .signal-head div {
    display: grid;
    gap: 0.2rem;
  }

  .signal-head span,
  .signal-metrics,
  .empty {
    color: var(--muted);
  }

  .signal-head button {
    border: 1px solid rgba(77, 211, 168, 0.28);
    border-radius: 0.65rem;
    background: rgba(77, 211, 168, 0.1);
    color: var(--text);
    cursor: pointer;
    font: inherit;
    font-size: 0.78rem;
    font-weight: 700;
    padding: 0.5rem 0.65rem;
  }

  .signal-head button:disabled {
    cursor: not-allowed;
    opacity: 0.46;
  }

  .signals p,
  .empty {
    margin: 0.45rem 0 0;
    line-height: 1.5;
  }

  .signal-metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 0.7rem;
    font-size: 0.82rem;
    margin-top: 0.55rem;
  }

  @media (max-width: 700px) {
    .signal-filters {
      grid-template-columns: 1fr;
    }

    .signal-filters .toggle {
      padding-bottom: 0;
    }
  }
</style>
