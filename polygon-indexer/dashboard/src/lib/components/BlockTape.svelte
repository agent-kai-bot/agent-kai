<script>
  import { flip } from 'svelte/animate';
  import { formatInteger, formatTimeAgo, formatTimestamp } from '$lib/utils/format';

  export let blocks = [];

  let hovered = null;

  $: orderedBlocks = [...blocks].reverse();
  $: maxTransfers = Math.max(...orderedBlocks.map((block) => Number(block.transfer_count || 0)), 1);

  function showTooltip(block, event) {
    const rect = event.currentTarget.getBoundingClientRect();
    hovered = {
      block,
      left: rect.left + rect.width / 2,
      top: rect.top - 8
    };
  }

  function hideTooltip() {
    hovered = null;
  }
</script>

<section class="block-tape glass-strip">
  <div class="scroller thin-scrollbar">
    <div class="cells">
      {#each orderedBlocks as block (block.block_number)}
        <button
          animate:flip={{ duration: 180 }}
          class="cell"
          type="button"
          on:mouseenter={(event) => showTooltip(block, event)}
          on:mouseleave={hideTooltip}
          aria-label={`Block ${block.block_number}`}
        >
          <svg width="14" height="28" viewBox="0 0 14 28" aria-hidden="true">
            <rect
              x="0.5"
              y="0.5"
              width="13"
              height="27"
              rx="4"
              fill="rgba(94, 231, 255, 1)"
              fill-opacity={0.05 + (Number(block.transfer_count || 0) / maxTransfers) * 0.55}
              stroke="rgba(148, 163, 184, 0.16)"
            />
            <rect
              x="3"
              y={26 - Math.max(2, (Number(block.gas_used_pct || 0) / 100) * 22)}
              width="8"
              height={Math.max(2, (Number(block.gas_used_pct || 0) / 100) * 22)}
              rx="2"
              fill="rgba(245, 158, 11, 0.25)"
            />
          </svg>
        </button>
      {/each}
    </div>
  </div>

  {#if hovered}
    <div class="tooltip" style:left={`${hovered.left}px`} style:top={`${hovered.top}px`}>
      <div class="tooltip-line">Block {formatInteger(hovered.block.block_number)}</div>
      <div class="tooltip-line">{formatTimestamp(hovered.block.timestamp)}</div>
      <div class="tooltip-line">tx {formatInteger(hovered.block.tx_count || 0)}</div>
      <div class="tooltip-line">xfers {formatInteger(hovered.block.transfer_count || 0)}</div>
      <div class="tooltip-line">gas {Number(hovered.block.gas_used_pct || 0).toFixed(1)}%</div>
      <div class="tooltip-time">{formatTimeAgo(hovered.block.timestamp)}</div>
    </div>
  {/if}
</section>

<style>
  .block-tape {
    position: sticky;
    top: var(--pulse-h);
    z-index: 18;
    height: var(--tape-h);
    display: flex;
    align-items: center;
    padding: 0 10px;
  }

  .scroller {
    width: 100%;
    overflow-x: auto;
    overflow-y: hidden;
  }

  .cells {
    display: flex;
    align-items: center;
    gap: 4px;
    min-width: max-content;
  }

  .cell {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 28px;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: pointer;
  }

  .tooltip {
    position: fixed;
    z-index: 30;
    transform: translate(-50%, -100%);
    display: grid;
    gap: 2px;
    padding: 8px 10px;
    border: 1px solid var(--border-strong);
    border-radius: 10px;
    background: rgba(8, 12, 20, 0.96);
    box-shadow: 0 14px 32px rgba(0, 0, 0, 0.36);
    pointer-events: none;
  }

  .tooltip-line,
  .tooltip-time {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    line-height: 1.2;
    color: var(--text-1);
  }

  .tooltip-time {
    color: var(--text-3);
  }
</style>
