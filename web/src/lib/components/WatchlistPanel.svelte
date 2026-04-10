<script lang="ts">
  import type { WatchlistQuote } from "$lib/daemon/types";

  import Panel from "$lib/components/Panel.svelte";

  let {
    quotes,
    mobileCollapsible = false,
    initiallyOpen = true,
  }: {
    quotes: WatchlistQuote[];
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
  } = $props();

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
  {#if quotes.length}
    <ul class="watchlist">
      {#each quotes as quote (quote.symbol)}
        <li>
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
        </li>
      {/each}
    </ul>
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
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    border: 1px solid rgba(145, 181, 221, 0.08);
    border-radius: 0.9rem;
    background: rgba(7, 19, 31, 0.68);
    padding: 0.8rem;
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
    .watchlist li {
      flex-direction: column;
      align-items: flex-start;
    }

    .pricing {
      justify-items: start;
    }
  }
</style>
