<script lang="ts">
  import type { WatchlistQuote } from "$lib/daemon/types";
  import {
    filterWatchlistQuotes,
    normalizeMarketSymbol,
    sortWatchlistQuotes,
    type WatchlistSortMode,
  } from "$lib/market-ui";

  import Panel from "$lib/components/Panel.svelte";

  let {
    quotes,
    activeSymbol = "",
    signalCounts = {},
    onSelect,
    onAddSymbol,
    onRemoveSymbol,
    mobileCollapsible = false,
    initiallyOpen = true,
  }: {
    quotes: WatchlistQuote[];
    activeSymbol?: string;
    signalCounts?: Record<string, number>;
    onSelect?: (symbol: string) => void;
    onAddSymbol?: (symbol: string) => void;
    onRemoveSymbol?: (symbol: string) => void;
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
  } = $props();

  let query = $state("");
  let addDraft = $state("");
  let sortMode = $state<WatchlistSortMode>("manual");

  function visibleQuotes(): WatchlistQuote[] {
    return sortWatchlistQuotes(
      filterWatchlistQuotes(quotes, query),
      sortMode,
      signalCounts,
    );
  }

  function addSymbol(): void {
    const symbol = normalizeMarketSymbol(addDraft);
    if (!symbol) {
      return;
    }
    addDraft = "";
    onAddSymbol?.(symbol);
  }

  function onAddSubmit(event: SubmitEvent): void {
    event.preventDefault();
    addSymbol();
  }

  function formatPrice(value?: number): string {
    return typeof value === "number" ? `$${value.toLocaleString()}` : "--";
  }

  function formatVolume(value?: number): string {
    return typeof value === "number" ? value.toLocaleString() : "--";
  }

  function formatChange(value?: number): string {
    if (typeof value !== "number") {
      return "--";
    }
    return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
  }

  function changeTone(value?: number): string {
    if (typeof value !== "number") {
      return "flat";
    }
    if (value > 0) {
      return "up";
    }
    if (value < 0) {
      return "down";
    }
    return "flat";
  }
</script>

<Panel
  eyebrow="Market"
  initiallyOpen={initiallyOpen}
  mobileCollapsible={mobileCollapsible}
  title="Watchlist"
  subtitle={`${quotes.length} tracked`}
>
  <div class="watchlist-controls">
    <label>
      <span>Search</span>
      <input bind:value={query} placeholder="BTC" type="search" />
    </label>
    <label>
      <span>Sort</span>
      <select bind:value={sortMode}>
        <option value="manual">Manual</option>
        <option value="signals">Signals</option>
        <option value="change">24h</option>
        <option value="volume">Volume</option>
        <option value="price">Price</option>
      </select>
    </label>
  </div>
  <form class="watchlist-add" onsubmit={onAddSubmit}>
    <input bind:value={addDraft} placeholder="Add symbol" type="search" />
    <button type="submit">Add</button>
  </form>
  {#if quotes.length}
    <ul class="watchlist">
      {#each visibleQuotes() as quote (quote.symbol)}
        <li class:active={quote.symbol === activeSymbol}>
          <article>
            <button
              aria-label={`Show ${quote.symbol} chart`}
              class="quote-button"
              onclick={() => onSelect?.(quote.symbol)}
              type="button"
            >
              <div class="identity">
                <strong>{quote.symbol}</strong>
                <span>{formatVolume(quote.volume_24h)} vol</span>
              </div>
              <div class="pricing">
                <strong>{formatPrice(quote.price)}</strong>
                <span class={changeTone(quote.price_change_24h_pct)}>
                  {formatChange(quote.price_change_24h_pct)}
                </span>
              </div>
            </button>
            <div class="quote-actions">
              {#if signalCounts[quote.symbol]}
                <span>{signalCounts[quote.symbol]} signals</span>
              {:else}
                <span>live</span>
              {/if}
              <button
                aria-label={`Remove ${quote.symbol} from watchlist`}
                onclick={() => onRemoveSymbol?.(quote.symbol)}
                type="button"
              >
                Remove
              </button>
            </div>
          </article>
        </li>
      {/each}
    </ul>
    {#if !visibleQuotes().length}
      <p class="empty">No tracked symbols match this filter.</p>
    {/if}
  {:else}
    <p class="empty">No watchlist symbols in the attached session.</p>
  {/if}
</Panel>

<style>
  .watchlist {
    display: grid;
    gap: 0.6rem;
    list-style: none;
    margin: 0;
    padding: 0;
    min-height: 0;
  }

  .watchlist li {
    border-radius: 0.9rem;
  }

  .watchlist-controls {
    display: grid;
    gap: 0.55rem;
    grid-template-columns: minmax(0, 1fr) auto;
    margin-bottom: 0.65rem;
  }

  .watchlist-controls label {
    display: grid;
    gap: 0.3rem;
  }

  .watchlist-controls span {
    color: var(--muted);
    font-size: 0.76rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  .watchlist-controls input,
  .watchlist-controls select,
  .watchlist-add input {
    width: 100%;
    border: 1px solid rgba(145, 181, 221, 0.16);
    border-radius: 0.65rem;
    background: rgba(7, 19, 31, 0.86);
    color: inherit;
    font: inherit;
    padding: 0.6rem 0.7rem;
  }

  .watchlist-add {
    display: grid;
    gap: 0.5rem;
    grid-template-columns: minmax(0, 1fr) auto;
    margin-bottom: 0.75rem;
  }

  .watchlist-add button,
  .quote-actions button {
    border: 1px solid rgba(145, 181, 221, 0.16);
    border-radius: 0.65rem;
    background: rgba(7, 19, 31, 0.72);
    color: inherit;
    cursor: pointer;
    font: inherit;
    font-size: 0.78rem;
    font-weight: 700;
    padding: 0.55rem 0.7rem;
  }

  .watchlist article {
    border: 1px solid rgba(145, 181, 221, 0.08);
    border-radius: 0.9rem;
    background: rgba(7, 19, 31, 0.68);
    overflow: hidden;
  }

  .quote-button {
    display: flex;
    width: 100%;
    justify-content: space-between;
    gap: 1rem;
    border: 0;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    padding: 0.8rem;
    text-align: left;
  }

  .quote-button:hover,
  .quote-button:focus-visible,
  .watchlist-add button:hover,
  .quote-actions button:hover,
  .watchlist-add button:focus-visible,
  .quote-actions button:focus-visible {
    outline: 2px solid rgba(77, 211, 168, 0.28);
    outline-offset: 2px;
  }

  .watchlist li.active article {
    border-color: rgba(77, 211, 168, 0.48);
    background: rgba(77, 211, 168, 0.1);
  }

  .identity,
  .pricing {
    display: grid;
    gap: 0.2rem;
  }

  .identity span,
  .pricing span,
  .empty {
    color: var(--muted);
  }

  .pricing {
    justify-items: end;
  }

  .quote-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    border-top: 1px solid rgba(145, 181, 221, 0.08);
    color: var(--muted);
    font-size: 0.78rem;
    padding: 0.55rem 0.8rem;
  }

  .up {
    color: #4dd3a8;
  }

  .down {
    color: #ff8f8f;
  }

  .flat {
    color: var(--muted);
  }

  @media (max-width: 700px) {
    .watchlist-controls,
    .watchlist-add {
      grid-template-columns: 1fr;
    }

    .quote-button {
      flex-direction: column;
      align-items: flex-start;
    }

    .pricing {
      justify-items: start;
    }
  }
</style>
