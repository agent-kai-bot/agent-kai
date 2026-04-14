<script>
  import { createEventDispatcher } from 'svelte';
  import { formatCompactNumber, formatPercent } from '$lib/utils/format';

  export let tokens = [];
  export let selectedAddress = null;

  const dispatch = createEventDispatcher();

  function selectToken(address) {
    dispatch('select', { address });
  }
</script>

<section class="panel token-rail">
  <div class="rail-header">
    <span class="eyebrow">Tracked Tokens</span>
    <span class="count">{tokens.length}</span>
  </div>

  <div class="rail-body thin-scrollbar">
    {#if tokens.length}
      {#each tokens as token}
        <button
          class:active={selectedAddress === token.contract_address}
          class="token-card"
          type="button"
          on:click={() => selectToken(token.contract_address)}
        >
          <span class="active-rail"></span>
          <div class="token-head">
            <div>
              <div class="symbol">{token.symbol}</div>
              <div class="name">{token.name || token.contract_address}</div>
            </div>
            <div class="metric-value mono-tabular">{formatCompactNumber(token.transfers_24h || 0)}</div>
          </div>

          <div class="token-meta">
            <span>xfers 24h</span>
            <span class="mono-tabular">{formatCompactNumber(token.total_holders || 0)} holders</span>
          </div>

          <div class="concentration">
            <div class="track">
              <div class="fill" style:width={`${Math.min(Number(token.top10_concentration_pct || 0), 100)}%`}></div>
            </div>
            <span class="mono-tabular">{formatPercent(token.top10_concentration_pct || 0, 1)} top10</span>
          </div>
        </button>
      {/each}
    {:else}
      <div class="empty">No tracked tokens yet.</div>
    {/if}
  </div>
</section>

<style>
  .token-rail {
    display: flex;
    flex-direction: column;
    min-height: 240px;
    overflow: hidden;
    padding: 14px;
  }

  .rail-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
  }

  .eyebrow {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-3);
  }

  .count {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    color: var(--text-2);
  }

  .rail-body {
    display: flex;
    flex-direction: row;
    gap: 12px;
    overflow-x: auto;
    overflow-y: hidden;
    padding-bottom: 2px;
  }

  .token-card {
    position: relative;
    display: grid;
    gap: 10px;
    width: min(280px, 72vw);
    min-height: 84px;
    padding: 14px 14px 14px 18px;
    border: 1px solid transparent;
    border-radius: 16px;
    background: rgba(8, 14, 22, 0.7);
    color: inherit;
    text-align: left;
    cursor: pointer;
    transition:
      border-color 180ms var(--ease-sharp),
      background-color 180ms var(--ease-sharp);
  }

  .token-card:hover,
  .token-card.active {
    border-color: rgba(94, 231, 255, 0.18);
    background: rgba(10, 18, 28, 0.92);
  }

  .active-rail {
    position: absolute;
    left: 0;
    top: 10px;
    bottom: 10px;
    width: 3px;
    border-radius: 999px;
    background: transparent;
    transition: background-color 180ms var(--ease-sharp);
  }

  .token-card.active .active-rail {
    background: var(--accent-live);
  }

  .token-head {
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: 10px;
  }

  .symbol {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 15px;
    font-weight: 600;
    line-height: 1.1;
    color: var(--text-1);
  }

  .name {
    margin-top: 4px;
    font-size: 12px;
    line-height: 1.2;
    color: var(--text-2);
  }

  .metric-value,
  .concentration span {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    font-weight: 500;
    color: var(--text-1);
  }

  .token-meta {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    font-size: 12px;
    color: var(--text-3);
  }

  .concentration {
    display: grid;
    gap: 6px;
  }

  .track {
    height: 7px;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.08);
    overflow: hidden;
  }

  .fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, rgba(94, 231, 255, 0.4), rgba(94, 231, 255, 0.92));
  }

  .empty {
    padding: 18px;
    border-radius: 14px;
    background: rgba(8, 14, 22, 0.72);
    color: var(--text-3);
  }

  @media (min-width: 1280px) {
    .rail-body {
      flex-direction: column;
      overflow-x: hidden;
      overflow-y: auto;
    }

    .token-card {
      width: 100%;
    }
  }
</style>
