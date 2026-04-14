<script>
  import { cubicOut } from 'svelte/easing';
  import { fly } from 'svelte/transition';
  import { tick } from 'svelte';
  import { formatAddress, formatTimeAgo, formatTokenAmount, formatUsd } from '$lib/utils/format';

  export let whales = [];

  let paused = false;
  let feedBody;

  $: if (feedBody && whales.length && !paused) {
    tick().then(() => {
      feedBody.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  function tier(usdValue) {
    const numeric = Number(usdValue);
    if (!Number.isFinite(numeric)) {
      return 'raw';
    }
    if (numeric >= 1000000) {
      return 'critical';
    }
    if (numeric >= 100000) {
      return 'alert';
    }
    if (numeric >= 10000) {
      return 'live';
    }
    return 'raw';
  }
</script>

<section class="panel whale-feed">
  <div class="feed-header">
    <div>
      <div class="eyebrow">Whale Flow</div>
      <h1>Live transfer surveillance</h1>
    </div>
    <div class="feed-meta">max 30</div>
  </div>

  <div
    bind:this={feedBody}
    class="feed-body thin-scrollbar"
    on:mouseenter={() => (paused = true)}
    on:mouseleave={() => (paused = false)}
  >
    {#if whales.length}
      {#each whales as whale (`${whale.tx_hash}-${whale.contract_address}-${whale.value}-${whale.timestamp}`)}
        <article
          transition:fly={{ y: 6, duration: 180, easing: cubicOut, opacity: 0 }}
          class={`feed-row tier-${tier(whale.usd_value)} ${Number(whale.usd_value || 0) >= 100000 ? 'tall' : ''}`}
        >
          <div class="accent-rail"></div>

          <div class="pill">{whale.token_symbol || 'TOKEN'}</div>

          <div class="route mono-tabular">
            <span>{formatAddress(whale.from_address)}</span>
            <span class="arrow">→</span>
            <span>{formatAddress(whale.to_address)}</span>
          </div>

          <div class="amount mono-tabular">
            <span class="primary">{formatUsd(whale.usd_value) || formatTokenAmount(whale.amount_human)}</span>
            {#if whale.usd_value !== null && whale.usd_value !== undefined}
              <span class="secondary">{formatTokenAmount(whale.amount_human)} {whale.token_symbol}</span>
            {/if}
          </div>

          <div class="time">{formatTimeAgo(whale.timestamp)}</div>
        </article>
      {/each}
    {:else}
      <div class="empty">Waiting for the first whale transfer.</div>
    {/if}
  </div>
</section>

<style>
  .whale-feed {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 360px;
    padding: 14px;
    overflow: hidden;
  }

  .feed-header {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 14px;
  }

  .eyebrow {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-3);
  }

  h1 {
    margin: 4px 0 0;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 19px;
    font-weight: 600;
    letter-spacing: -0.02em;
  }

  .feed-meta {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: var(--text-3);
  }

  .feed-body {
    display: flex;
    flex-direction: column;
    gap: 8px;
    overflow: auto;
    min-height: 0;
  }

  .feed-row {
    position: relative;
    display: grid;
    grid-template-columns: 84px minmax(0, 1fr) minmax(84px, 112px) 64px;
    align-items: center;
    gap: 12px;
    min-height: 52px;
    padding: 12px 14px 12px 16px;
    border: 1px solid rgba(148, 163, 184, 0.08);
    border-radius: 16px;
    background: rgba(9, 14, 22, 0.84);
  }

  .feed-row.tall {
    min-height: 64px;
  }

  .accent-rail {
    position: absolute;
    inset: 10px auto 10px 0;
    width: 3px;
    border-radius: 999px;
    background: transparent;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: fit-content;
    min-width: 62px;
    height: 28px;
    padding: 0 10px;
    border-radius: 999px;
    background: rgba(94, 231, 255, 0.08);
    border: 1px solid rgba(94, 231, 255, 0.16);
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-1);
  }

  .route {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: var(--text-2);
  }

  .arrow {
    color: var(--text-3);
  }

  .amount {
    display: grid;
    justify-items: end;
    gap: 3px;
    text-align: right;
  }

  .primary {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-1);
  }

  .secondary {
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 11px;
    color: var(--text-3);
  }

  .time {
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 12px;
    color: var(--text-3);
    text-align: right;
  }

  .tier-live {
    border-color: rgba(94, 231, 255, 0.16);
  }

  .tier-live .accent-rail {
    background: var(--accent-live);
  }

  .tier-alert {
    border-color: rgba(255, 77, 141, 0.18);
  }

  .tier-alert .accent-rail {
    background: var(--accent-alert);
  }

  .tier-critical {
    border-color: rgba(255, 77, 141, 0.28);
    background: linear-gradient(180deg, rgba(37, 14, 24, 0.92), rgba(17, 10, 15, 0.94));
  }

  .tier-critical .accent-rail {
    background: var(--accent-alert);
  }

  .tier-critical .primary {
    font-weight: 700;
  }

  .empty {
    padding: 22px;
    border-radius: 14px;
    background: rgba(8, 14, 22, 0.72);
    color: var(--text-3);
  }

  @media (max-width: 880px) {
    .feed-row {
      grid-template-columns: 1fr auto;
      gap: 8px 12px;
    }

    .pill {
      order: 1;
    }

    .amount {
      order: 2;
    }

    .route {
      order: 3;
      grid-column: 1 / -1;
    }

    .time {
      order: 4;
    }
  }
</style>
