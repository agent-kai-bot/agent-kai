<script lang="ts">
  import type { PortfolioSnapshot } from "$lib/daemon/types";

  import Panel from "$lib/components/Panel.svelte";

  let {
    portfolio,
    mobileCollapsible = false,
    initiallyOpen = true,
  }: {
    portfolio: PortfolioSnapshot;
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
  } = $props();

  function money(value?: number): string {
    return typeof value === "number" ? `$${value.toLocaleString()}` : "--";
  }

  function signedPct(value?: number): string {
    return typeof value === "number" ? `${value >= 0 ? "+" : ""}${value.toFixed(1)}%` : "--";
  }
</script>

<Panel
  eyebrow="Portfolio"
  initiallyOpen={initiallyOpen}
  mobileCollapsible={mobileCollapsible}
  title="Positions"
  subtitle={`${portfolio.positions.length} open`}
>
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
            <td data-label="Symbol">{position.symbol}</td>
            <td data-label="Side">{position.side}</td>
            <td data-label="Qty">{position.quantity.toFixed(4)}</td>
            <td
              class:gain={position.unrealized_pnl > 0}
              class:loss={position.unrealized_pnl < 0}
              data-label="P&L"
            >
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

  @media (max-width: 700px) {
    thead {
      display: none;
    }

    table,
    tbody,
    tr {
      display: block;
    }

    tr {
      border: 1px solid rgba(145, 181, 221, 0.08);
      border-radius: 0.9rem;
      background: rgba(7, 19, 31, 0.68);
      padding: 0.75rem 0.8rem;
    }

    tr + tr {
      margin-top: 0.7rem;
    }

    td {
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      border-bottom: 0;
      padding: 0.25rem 0;
      text-align: left;
    }

    td:last-child {
      text-align: left;
    }

    td::before {
      content: attr(data-label);
      color: var(--muted);
      font-size: 0.76rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
  }
</style>
