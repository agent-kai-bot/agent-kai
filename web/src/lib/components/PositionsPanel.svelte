<script lang="ts">
  import type { PortfolioSnapshot } from "$lib/daemon/types";

  import Panel from "$lib/components/Panel.svelte";

  let {
    portfolio,
  }: {
    portfolio: PortfolioSnapshot;
  } = $props();

  function money(value?: number): string {
    return typeof value === "number" ? `$${value.toLocaleString()}` : "--";
  }

  function signedPct(value?: number): string {
    return typeof value === "number" ? `${value >= 0 ? "+" : ""}${value.toFixed(1)}%` : "--";
  }
</script>

<Panel eyebrow="Portfolio" title="Positions" subtitle={`${portfolio.positions.length} open`}>
  {#if portfolio.positions.length}
    <table>
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Side</th>
          <th>Qty</th>
          <th>P&L</th>
        </tr>
      </thead>
      <tbody>
        {#each portfolio.positions as position (position.symbol)}
          <tr>
            <td>{position.symbol}</td>
            <td>{position.side}</td>
            <td>{position.quantity.toFixed(4)}</td>
            <td class:loss={position.unrealized_pnl < 0} class:gain={position.unrealized_pnl > 0}>
              {money(position.unrealized_pnl)} · {signedPct(position.pnl_pct)}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    <footer>
      <span>Total value</span>
      <strong>{money(portfolio.pnl.total_value as number | undefined)}</strong>
    </footer>
  {:else}
    <p class="empty">No open paper positions.</p>
  {/if}
</Panel>

<style>
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.92rem;
  }

  th,
  td {
    border-bottom: 1px solid rgba(145, 181, 221, 0.08);
    padding: 0.55rem 0;
    text-align: left;
  }

  th {
    color: var(--muted);
    font-size: 0.76rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  td:last-child,
  th:last-child {
    text-align: right;
  }

  .gain {
    color: #4dd3a8;
  }

  .loss {
    color: #ff8f8f;
  }

  footer {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    margin-top: 0.9rem;
    color: var(--muted);
  }

  footer strong {
    color: var(--text);
  }

  .empty {
    color: var(--muted);
    margin: 0;
  }
</style>
