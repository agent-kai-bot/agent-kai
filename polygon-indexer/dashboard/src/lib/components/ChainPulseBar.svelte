<script>
  import { onDestroy } from 'svelte';
  import { formatInteger, formatNumeric, formatPercent } from '$lib/utils/format';

  export let overview = null;
  export let flashKey = 0;
  export let connected = false;

  let flashing = false;
  let flashTimer;

  $: if (flashKey) {
    clearTimeout(flashTimer);
    flashing = true;
    flashTimer = setTimeout(() => {
      flashing = false;
    }, 120);
  }

  $: lag = overview?.head_lag_blocks;
  $: lagColor =
    lag === 0
      ? 'var(--accent-success)'
      : lag > 10
        ? 'var(--accent-danger)'
        : 'var(--accent-warn)';

  onDestroy(() => {
    clearTimeout(flashTimer);
  });
</script>

<section class:flash-active={flashing} class="chain-pulse glass-strip">
  <div class="flash-wash"></div>
  <div class="inner">
    <div class="network-chip">
      <span class="network-name">Polygon</span>
      <span class="status-dot" style:background={connected ? lagColor : 'var(--text-3)'}></span>
    </div>

    <div class="metric">
      <span class="label">Block</span>
      <span class="value mono-tabular">{formatInteger(overview?.last_indexed_block || 0)}</span>
    </div>

    <div class="metric">
      <span class="label">TPS</span>
      <span class="value mono-tabular">{formatNumeric(overview?.tps_current || 0, 1)}</span>
    </div>

    <div class="metric">
      <span class="label">Gas</span>
      <span class="value mono-tabular">{formatNumeric(overview?.gas_current_gwei || 0, 1)} gwei</span>
    </div>

    <div class="metric">
      <span class="label">Lag</span>
      <span class="value mono-tabular">{formatInteger(lag || 0)}</span>
    </div>

    <div class="metric">
      <span class="label">Indexed</span>
      <span class="value mono-tabular">{formatPercent(overview?.backfill_pct || 0, 0)}</span>
    </div>
  </div>
</section>

<style>
  .chain-pulse {
    position: sticky;
    top: 0;
    z-index: 20;
    height: var(--pulse-h);
    overflow: hidden;
  }

  .inner {
    position: relative;
    z-index: 2;
    display: flex;
    height: 100%;
    align-items: center;
    gap: 14px;
    padding: 0 14px;
    overflow-x: auto;
  }

  .network-chip {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding-right: 14px;
    margin-right: 4px;
    border-right: 1px solid var(--border-subtle);
  }

  .network-name {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 15px;
    font-weight: 600;
    color: var(--text-1);
  }

  .status-dot {
    width: 9px;
    height: 9px;
    border-radius: 999px;
    box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.08);
  }

  .metric {
    display: grid;
    gap: 4px;
    min-width: max-content;
  }

  .label {
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 11px;
    font-weight: 600;
    line-height: 1;
    letter-spacing: 0.11em;
    text-transform: uppercase;
    color: var(--text-3);
  }

  .value {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 14px;
    font-weight: 500;
    line-height: 1;
    color: var(--text-1);
  }

  .flash-wash {
    position: absolute;
    inset: 0;
    transform: translateX(-105%);
    opacity: 0;
    background: linear-gradient(90deg, transparent, rgba(94, 231, 255, 0.22), transparent);
    pointer-events: none;
  }

  .flash-active .flash-wash {
    animation: pulse-wash 120ms var(--ease-sharp);
  }

  @keyframes pulse-wash {
    0% {
      opacity: 0;
      transform: translateX(-105%);
    }

    25% {
      opacity: 1;
    }

    100% {
      opacity: 0;
      transform: translateX(105%);
    }
  }
</style>
