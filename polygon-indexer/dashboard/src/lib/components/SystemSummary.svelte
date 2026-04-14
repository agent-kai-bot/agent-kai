<script>
  import { cubicOut } from 'svelte/easing';
  import { tweened } from 'svelte/motion';
  import { formatInteger, formatPercent, formatTimestamp } from '$lib/utils/format';

  export let overview = null;
  export let connected = false;
  export let error = null;

  const counter = tweened(0, { duration: 480, easing: cubicOut });

  $: counter.set(Number(overview?.total_transfers_indexed || 0));
</script>

<section class="panel system-summary">
  <div class="header">
    <div>
      <div class="eyebrow">System Summary</div>
      <h2>Indexer state</h2>
    </div>
    <div class:offline={!connected} class="badge">{connected ? 'live' : 'retrying'}</div>
  </div>

  <div class="grid">
    <div class="stat">
      <span class="label">Indexed block</span>
      <span class="value mono-tabular">{formatInteger(overview?.last_indexed_block || 0)}</span>
    </div>
    <div class="stat">
      <span class="label">Chain head</span>
      <span class="value mono-tabular">{formatInteger(overview?.chain_head || 0)}</span>
    </div>
    <div class="stat">
      <span class="label">Lag blocks</span>
      <span class="value mono-tabular">{formatInteger(overview?.head_lag_blocks || 0)}</span>
    </div>
    <div class="stat">
      <span class="label">Transfers indexed</span>
      <span class="value mono-tabular">{formatInteger($counter)}</span>
    </div>
  </div>

  {#if overview && !overview.backfill_complete}
    <div class="progress-wrap">
      <div class="progress-label">
        <span>Backfill</span>
        <span class="mono-tabular">{formatPercent(overview.backfill_pct || 0, 0)}</span>
      </div>
      <div class="progress-track">
        <div class="progress-fill" style:width={`${Math.min(Number(overview.backfill_pct || 0), 100)}%`}></div>
      </div>
    </div>
  {/if}

  <div class="footer">
    <div class="stamp">
      <span class="label">Last updated</span>
      <span class="stamp-value mono-tabular">{formatTimestamp(overview?.last_updated_at)}</span>
    </div>

    {#if error}
      <div class="error">{error}</div>
    {/if}
  </div>
</section>

<style>
  .system-summary {
    display: grid;
    gap: 16px;
    padding: 16px;
  }

  .header {
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: 12px;
  }

  .eyebrow {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-3);
  }

  h2 {
    margin: 4px 0 0;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 17px;
    font-weight: 600;
  }

  .badge {
    min-width: 70px;
    height: 28px;
    padding: 0 10px;
    border-radius: 999px;
    background: rgba(34, 197, 94, 0.14);
    color: var(--accent-success);
    border: 1px solid rgba(34, 197, 94, 0.22);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
  }

  .badge.offline {
    background: rgba(245, 158, 11, 0.14);
    color: var(--accent-warn);
    border-color: rgba(245, 158, 11, 0.22);
  }

  .grid {
    display: grid;
    gap: 12px;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .stat {
    display: grid;
    gap: 4px;
    padding: 12px;
    border-radius: 14px;
    background: rgba(8, 14, 22, 0.74);
    border: 1px solid rgba(148, 163, 184, 0.08);
  }

  .label {
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 12px;
    font-weight: 500;
    color: var(--text-3);
  }

  .value,
  .stamp-value {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-1);
  }

  .progress-wrap {
    display: grid;
    gap: 8px;
  }

  .progress-label {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    color: var(--text-2);
  }

  .progress-track {
    height: 9px;
    border-radius: 999px;
    overflow: hidden;
    background: rgba(148, 163, 184, 0.08);
  }

  .progress-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, rgba(94, 231, 255, 0.3), rgba(94, 231, 255, 1));
    transition: width 180ms var(--ease-sharp);
  }

  .footer {
    display: grid;
    gap: 8px;
  }

  .stamp {
    display: grid;
    gap: 4px;
  }

  .error {
    font-size: 12px;
    color: var(--accent-alert);
  }
</style>
