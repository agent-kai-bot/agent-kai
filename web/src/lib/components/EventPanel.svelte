<script lang="ts">
  import Panel from "$lib/components/Panel.svelte";

  export type EventRow = {
    id?: string;
    headline: string;
    detail: string;
    timestamp?: string;
    relativeTime?: string;
    symbol?: string;
    side?: string;
    score?: number;
    price?: string;
    timeframe?: string;
    rsi?: string;
    macd?: string;
    source?: string;
    tone?: "neutral" | "positive" | "warning" | "danger";
  };

  let {
    eyebrow = "",
    title,
    subtitle = "",
    emptyMessage,
    items,
    onSelect,
    mobileCollapsible = false,
    initiallyOpen = true,
  }: {
    eyebrow?: string;
    title: string;
    subtitle?: string;
    emptyMessage: string;
    items: EventRow[];
    onSelect?: (item: EventRow) => void;
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
  } = $props();
</script>

<Panel
  {eyebrow}
  {title}
  {subtitle}
  initiallyOpen={initiallyOpen}
  mobileCollapsible={mobileCollapsible}
>
  {#if items.length}
    <ul class="events">
      {#each items as item, index (item.id ?? item.headline + item.detail + index)}
        <li class={item.tone ?? "neutral"}>
          <button
            aria-disabled={!onSelect}
            aria-label={`Inspect ${item.symbol ?? item.headline}`}
            class:clickable={Boolean(onSelect)}
            onclick={() => onSelect?.(item)}
            tabindex={onSelect ? 0 : -1}
            type="button"
          >
            <div class="event-head">
              <strong>{item.headline}</strong>
              {#if item.timestamp || item.relativeTime}
                <span>{item.timestamp}{item.relativeTime ? ` · ${item.relativeTime}` : ""}</span>
              {/if}
            </div>
            <p>{item.detail}</p>
            {#if item.price || item.rsi || item.macd || item.timeframe || item.source}
              <div class="metrics">
                {#if item.price}<span>price {item.price}</span>{/if}
                {#if item.rsi}<span>RSI {item.rsi}</span>{/if}
                {#if item.macd}<span>MACD {item.macd}</span>{/if}
                {#if item.timeframe}<span>tf {item.timeframe}</span>{/if}
                {#if item.source}<span>{item.source}</span>{/if}
              </div>
            {/if}
          </button>
        </li>
      {/each}
    </ul>
  {:else}
    <p class="empty">{emptyMessage}</p>
  {/if}
</Panel>

<style>
  .events {
    display: grid;
    gap: 0.65rem;
    list-style: none;
    margin: 0;
    padding: 0;
    min-height: 0;
  }

  .events li {
    border: 1px solid rgba(145, 181, 221, 0.08);
    border-radius: 0.9rem;
    background: rgba(7, 19, 31, 0.68);
  }

  .events button {
    width: 100%;
    border: 0;
    background: transparent;
    color: inherit;
    cursor: default;
    font: inherit;
    padding: 0.8rem;
    text-align: left;
  }

  .events button.clickable {
    cursor: pointer;
  }

  .events button.clickable:hover,
  .events button.clickable:focus-visible {
    outline: 2px solid rgba(77, 211, 168, 0.28);
    outline-offset: 2px;
  }

  .events li.positive {
    border-color: rgba(77, 211, 168, 0.2);
  }

  .events li.warning {
    border-color: rgba(243, 196, 106, 0.2);
  }

  .events li.danger {
    border-color: rgba(255, 143, 143, 0.24);
  }

  .event-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.3rem;
  }

  .event-head span,
  .metrics {
    color: var(--muted);
    font-size: 0.82rem;
  }

  .events p,
  .empty {
    margin: 0;
    color: var(--muted);
    line-height: 1.5;
  }

  .metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 0.7rem;
    margin-top: 0.55rem;
  }
</style>
