<script>
  import { fade, fly } from 'svelte/transition';
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { formatAddress, formatInteger, formatPercent, formatTimeAgo, formatTokenAmount } from '$lib/utils/format';

  export let inspector = null;

  const dispatch = createEventDispatcher();

  function close() {
    dispatch('close');
  }

  async function copyAddress(address) {
    if (!address || !navigator?.clipboard) {
      return;
    }
    await navigator.clipboard.writeText(address);
  }

  function handleKeydown(event) {
    if (event.key === 'Escape') {
      close();
    }
  }

  $: if (inspector?.open) {
    window.addEventListener('keydown', handleKeydown);
  } else {
    window.removeEventListener('keydown', handleKeydown);
  }

  onDestroy(() => {
    window.removeEventListener('keydown', handleKeydown);
  });
</script>

{#if inspector?.open}
  <div class="overlay-scrim" transition:fade={{ duration: 180 }} on:click={close}></div>

  <aside class="drawer-wrap">
    <section class="drawer panel" transition:fly={{ x: 420, duration: 220 }}>
      <header class="drawer-header">
        <button class="back-button" type="button" on:click={close}>Back</button>
        <button class="close-button" type="button" on:click={close} aria-label="Close inspector">×</button>
      </header>

      {#if inspector.loading}
        <div class="state">Loading token detail…</div>
      {:else if inspector.error}
        <div class="state error">{inspector.error}</div>
      {:else if inspector.token}
        <div class="content thin-scrollbar">
          <section class="section header-card">
            <div class="token-row">
              <div>
                <div class="symbol">{inspector.token.symbol}</div>
                <div class="name">{inspector.token.name || inspector.address}</div>
              </div>
              <button class="copy" type="button" on:click={() => copyAddress(inspector.token.contract_address)}>Copy</button>
            </div>
            <div class="address mono-tabular">{inspector.token.contract_address}</div>
          </section>

          <section class="section">
            <h3>Key metrics</h3>
            <div class="metric-grid">
              <div class="metric">
                <span class="label">Transfers 24h</span>
                <span class="value mono-tabular">{formatInteger(inspector.token.transfers_24h || 0)}</span>
              </div>
              <div class="metric">
                <span class="label">Holders</span>
                <span class="value mono-tabular">{formatInteger(inspector.token.total_holders || 0)}</span>
              </div>
              <div class="metric">
                <span class="label">Top10 concentration</span>
                <span class="value mono-tabular">
                  {formatPercent(inspector.token.top10_concentration_pct ?? inspector.token.top10_concentration ?? 0, 1)}
                </span>
              </div>
            </div>
          </section>

          <section class="section">
            <h3>Top 10 holders</h3>
            <div class="list">
              {#if inspector.holders.length}
                {#each inspector.holders as holder}
                  <div class="list-row">
                    <div class="row-main">
                      <div class="row-title mono-tabular">{formatAddress(holder.wallet_address)}</div>
                      <div class="row-sub mono-tabular">{holder.wallet_address}</div>
                    </div>
                    <div class="row-side">
                      <div class="holder-balance mono-tabular">{formatTokenAmount(holder.balance_human)}</div>
                      <div class="row-sub mono-tabular">{formatPercent(holder.pct_of_tracked || 0, 1)}</div>
                    </div>
                  </div>
                {/each}
              {:else}
                <div class="empty">No holder rows available.</div>
              {/if}
            </div>
          </section>

          <section class="section">
            <h3>Recent transfers</h3>
            <div class="list">
              {#if inspector.transfers.length}
                {#each inspector.transfers as transfer}
                  <div class="list-row transfer-row">
                    <div class="row-main">
                      <div class="row-title mono-tabular">
                        {formatAddress(transfer.from_address)} → {formatAddress(transfer.to_address)}
                      </div>
                      <div class="row-sub mono-tabular">{transfer.tx_hash}</div>
                    </div>
                    <div class="row-side">
                      <div class="holder-balance mono-tabular">
                        {formatTokenAmount(Number(transfer.value || 0) / Math.pow(10, inspector.token.decimals || 18))}
                      </div>
                      <div class="row-sub">{formatTimeAgo(transfer.timestamp)}</div>
                    </div>
                  </div>
                {/each}
              {:else}
                <div class="empty">No recent transfers in the selected window.</div>
              {/if}
            </div>
          </section>
        </div>
      {:else}
        <div class="state">Select a token to inspect.</div>
      {/if}
    </section>
  </aside>
{/if}

<style>
  .drawer-wrap {
    position: fixed;
    inset: 0 0 0 auto;
    z-index: 50;
    display: flex;
    justify-content: flex-end;
    pointer-events: none;
  }

  .drawer {
    pointer-events: auto;
    width: min(420px, 100vw);
    height: 100vh;
    border-radius: 0;
    border-left: 1px solid var(--border-strong);
    display: grid;
    grid-template-rows: auto 1fr;
    background:
      linear-gradient(180deg, rgba(12, 18, 28, 0.96), rgba(8, 12, 20, 0.96)),
      rgba(8, 12, 20, 0.96);
    backdrop-filter: blur(10px);
  }

  .drawer-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 16px;
    border-bottom: 1px solid var(--border-subtle);
  }

  .back-button,
  .copy,
  .close-button {
    border: 1px solid rgba(148, 163, 184, 0.14);
    background: rgba(9, 14, 22, 0.74);
    color: var(--text-1);
    border-radius: 999px;
    cursor: pointer;
  }

  .back-button,
  .copy {
    height: 32px;
    padding: 0 12px;
    font-size: 12px;
  }

  .close-button {
    width: 32px;
    height: 32px;
    font-size: 18px;
    line-height: 1;
  }

  .content {
    overflow: auto;
    padding: 16px;
    display: grid;
    gap: 14px;
  }

  .section {
    display: grid;
    gap: 12px;
    padding: 14px;
    border-radius: 16px;
    background: rgba(8, 14, 22, 0.74);
    border: 1px solid rgba(148, 163, 184, 0.08);
  }

  h3 {
    margin: 0;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 14px;
    font-weight: 600;
  }

  .header-card {
    gap: 10px;
  }

  .token-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: start;
  }

  .symbol {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 20px;
    font-weight: 600;
  }

  .name {
    font-size: 13px;
    color: var(--text-2);
  }

  .address {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: var(--text-3);
    word-break: break-all;
  }

  .metric-grid {
    display: grid;
    gap: 10px;
  }

  .metric {
    display: grid;
    gap: 4px;
  }

  .label {
    font-size: 12px;
    color: var(--text-3);
  }

  .value,
  .holder-balance {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    font-weight: 500;
    color: var(--text-1);
  }

  .list {
    display: grid;
    gap: 10px;
  }

  .list-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 10px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.08);
  }

  .row-main,
  .row-side {
    display: grid;
    gap: 4px;
  }

  .row-side {
    justify-items: end;
    text-align: right;
  }

  .row-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    color: var(--text-1);
  }

  .row-sub {
    font-size: 12px;
    color: var(--text-3);
  }

  .state,
  .empty {
    padding: 18px;
    color: var(--text-3);
  }

  .state.error {
    color: var(--accent-alert);
  }
</style>
