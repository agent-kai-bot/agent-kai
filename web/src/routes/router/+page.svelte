<script lang="ts">
  import { onDestroy, onMount } from "svelte";

  import { DaemonClient } from "$lib/daemon/client";
  import { readStoredToken } from "$lib/daemon/storage";
  import type {
    SignalRouterConfig,
    SignalRouterDecision,
    SignalRouterHealth,
    SignalRouterRoute,
  } from "$lib/daemon/types";

  const client = new DaemonClient();
  const emptyConfig: SignalRouterConfig = {
    mode: "legacy",
    live_trades_enabled: false,
    kill_switch_active: false,
    routes: [],
    last_decisions: [],
    dedup_stats: {
      keys_count: 0,
      cooldown_hits_24h: 0,
      cap_hits_24h: 0,
    },
  };

  let token = $state("");
  let config = $state<SignalRouterConfig>(emptyConfig);
  let health = $state<SignalRouterHealth | null>(null);
  let statusText = $state("loading");
  let errorText = $state("");
  let pendingLiveToggle = $state(false);
  let pendingRoute = $state("");
  let pollHandle: number | null = null;

  function stopPolling(): void {
    if (pollHandle !== null) {
      window.clearInterval(pollHandle);
      pollHandle = null;
    }
  }

  async function refresh(): Promise<void> {
    try {
      const [nextHealth, nextConfig] = await Promise.all([
        client.fetchSignalRouterHealth(token),
        client.fetchSignalRouterConfig(token),
      ]);
      health = nextHealth;
      config = nextConfig;
      statusText = `updated ${new Date().toLocaleTimeString()}`;
      errorText = "";
    } catch (error) {
      errorText = error instanceof Error ? error.message : String(error);
      statusText = "unavailable";
    }
  }

  async function setLiveTrades(enabled: boolean): Promise<void> {
    pendingLiveToggle = true;
    errorText = "";
    try {
      config = await client.updateSignalRouterLiveTrades(enabled, token);
      health = await client.fetchSignalRouterHealth(token);
    } catch (error) {
      errorText = error instanceof Error ? error.message : String(error);
      await refresh();
    } finally {
      pendingLiveToggle = false;
    }
  }

  async function setRouteEnabled(route: SignalRouterRoute, enabled: boolean): Promise<void> {
    pendingRoute = route.name;
    errorText = "";
    try {
      config = await client.updateSignalRouterRoute(route.name, enabled, token);
      health = await client.fetchSignalRouterHealth(token);
    } catch (error) {
      errorText = error instanceof Error ? error.message : String(error);
      await refresh();
    } finally {
      pendingRoute = "";
    }
  }

  function actionKinds(route: SignalRouterRoute): string {
    return route.actions.map((action) => action.kind).join(", ") || "none";
  }

  function recentDecisions(): SignalRouterDecision[] {
    const rows = config.last_decisions;
    if (Array.isArray(rows) && rows.length) {
      return rows.slice(0, 10);
    }
    return config.routes.flatMap((route) => route.last_decisions).slice(0, 10);
  }

  function shortTime(value: string): string {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value || "n/a";
    }
    return date.toLocaleString();
  }

  onMount(() => {
    token = readStoredToken();
    void refresh();
    pollHandle = window.setInterval(() => void refresh(), 5000);
  });

  onDestroy(stopPolling);
</script>

<svelte:head>
  <title>Signal Router</title>
</svelte:head>

<main class="router-shell">
  <header class="router-header">
    <div>
      <a class="back-link" href="/">Dashboard</a>
      <h1>Signal Router</h1>
    </div>
    <div class="header-status">
      <span class:legacy={config.mode === "legacy"} class:live={config.mode === "new"} class="mode-badge">
        {config.mode}
      </span>
      <span>{statusText}</span>
    </div>
  </header>

  {#if errorText}
    <p class="router-error">{errorText}</p>
  {/if}

  <section class:armed={config.live_trades_enabled} class="live-toggle-panel">
    <div>
      <span class="panel-label">Global execution</span>
      <strong>{config.live_trades_enabled ? "LIVE TRADES" : "DRY RUN"}</strong>
    </div>
    <label class="switch-control">
      <input
        checked={config.live_trades_enabled}
        disabled={pendingLiveToggle || config.kill_switch_active}
        onchange={(event) => void setLiveTrades(event.currentTarget.checked)}
        type="checkbox"
      />
      <span></span>
    </label>
  </section>

  <section class="router-grid">
    <div class="routes-panel">
      <div class="panel-heading">
        <h2>Routes</h2>
        <span>{health?.routes_enabled_count ?? 0} enabled / {health?.routes_disabled_count ?? 0} disabled</span>
      </div>
      <div class="routes-table">
        <div class="routes-row routes-head">
          <span>Name</span>
          <span>Channel</span>
          <span>Actions</span>
          <span>State</span>
          <span>24h</span>
          <span>Kill</span>
        </div>
        {#each config.routes as route (route.name)}
          <div class="routes-row">
            <strong>{route.name}</strong>
            <span>{route.channel}</span>
            <span>{actionKinds(route)}</span>
            <label class="mini-switch">
              <input
                checked={route.enabled}
                disabled={pendingRoute === route.name || config.kill_switch_active}
                onchange={(event) => void setRouteEnabled(route, event.currentTarget.checked)}
                type="checkbox"
              />
              <span>{route.enabled ? "enabled" : "disabled"}</span>
            </label>
            <span>{route.fire_count_24h} fired / {route.suppress_count_24h} suppressed</span>
            <span class:danger={config.kill_switch_active}>
              {config.kill_switch_active ? "active" : "clear"}
            </span>
          </div>
        {/each}
      </div>
    </div>

    <aside class="stats-panel">
      <section>
        <h2>Dedup Stats</h2>
        <dl>
          <div><dt>Keys</dt><dd>{config.dedup_stats.keys_count}</dd></div>
          <div><dt>Cooldown hits</dt><dd>{config.dedup_stats.cooldown_hits_24h}</dd></div>
          <div><dt>Cap hits</dt><dd>{config.dedup_stats.cap_hits_24h}</dd></div>
        </dl>
      </section>

      <section>
        <h2>Recent Decisions</h2>
        <div class="decision-list">
          {#each recentDecisions() as decision, index (`${decision.timestamp}-${decision.route}-${decision.kind}-${index}`)}
            <article>
              <strong>{decision.route}</strong>
              <span>{decision.kind} · {decision.status}</span>
              <time>{shortTime(decision.timestamp)}</time>
            </article>
          {:else}
            <p class="empty-state">No decisions recorded.</p>
          {/each}
        </div>
      </section>
    </aside>
  </section>
</main>

<style>
  :global(body) {
    overflow: auto;
  }

  .router-shell {
    display: grid;
    gap: 1rem;
    min-height: 100dvh;
    padding: 1rem;
  }

  .router-header,
  .live-toggle-panel,
  .routes-panel,
  .stats-panel section,
  .router-error {
    border: 1px solid rgba(145, 181, 221, 0.16);
    border-radius: 8px;
    background: rgba(5, 17, 28, 0.72);
    box-shadow: 0 1rem 2.5rem rgba(0, 0, 0, 0.22);
  }

  .router-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 1rem 1.2rem;
  }

  .back-link {
    color: var(--accent);
    font-size: 0.88rem;
    font-weight: 700;
    text-decoration: none;
  }

  h1,
  h2 {
    margin: 0;
    letter-spacing: 0;
  }

  h1 {
    margin-top: 0.25rem;
    font-size: 1.8rem;
  }

  h2 {
    font-size: 1rem;
  }

  .header-status {
    display: flex;
    align-items: center;
    color: var(--muted);
    gap: 0.7rem;
  }

  .mode-badge {
    border: 1px solid rgba(145, 181, 221, 0.18);
    border-radius: 999px;
    color: #f3c46a;
    font-size: 0.8rem;
    font-weight: 800;
    padding: 0.35rem 0.65rem;
    text-transform: uppercase;
  }

  .mode-badge.live {
    color: #4dd3a8;
  }

  .mode-badge.legacy {
    color: #ffb0b0;
  }

  .router-error {
    color: #ffb0b0;
    margin: 0;
    padding: 0.8rem 1rem;
  }

  .live-toggle-panel {
    align-items: center;
    display: flex;
    justify-content: space-between;
    min-height: 7rem;
    padding: 1.2rem;
  }

  .live-toggle-panel.armed {
    border-color: rgba(77, 211, 168, 0.45);
    background: rgba(12, 47, 39, 0.7);
  }

  .panel-label,
  .panel-heading span,
  .routes-head,
  .decision-list span,
  .decision-list time,
  .empty-state {
    color: var(--muted);
  }

  .panel-label {
    display: block;
    font-size: 0.82rem;
    font-weight: 800;
    margin-bottom: 0.4rem;
    text-transform: uppercase;
  }

  .live-toggle-panel strong {
    font-size: 1.7rem;
  }

  .switch-control input,
  .mini-switch input {
    position: absolute;
    opacity: 0;
    pointer-events: none;
  }

  .switch-control span {
    background: rgba(145, 181, 221, 0.18);
    border-radius: 999px;
    cursor: pointer;
    display: block;
    height: 3rem;
    position: relative;
    width: 6rem;
  }

  .switch-control span::after {
    background: var(--text);
    border-radius: 50%;
    content: "";
    height: 2.35rem;
    left: 0.35rem;
    position: absolute;
    top: 0.32rem;
    transition: transform 160ms ease;
    width: 2.35rem;
  }

  .switch-control input:checked + span {
    background: #4dd3a8;
  }

  .switch-control input:checked + span::after {
    transform: translateX(3rem);
  }

  .switch-control input:disabled + span,
  .mini-switch input:disabled + span {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .router-grid {
    display: grid;
    gap: 1rem;
    grid-template-columns: minmax(0, 1fr) minmax(18rem, 24rem);
  }

  .routes-panel,
  .stats-panel section {
    padding: 1rem;
  }

  .panel-heading {
    align-items: center;
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.8rem;
  }

  .routes-table {
    display: grid;
    gap: 0.35rem;
    overflow-x: auto;
  }

  .routes-row {
    align-items: center;
    border: 1px solid rgba(145, 181, 221, 0.1);
    border-radius: 8px;
    display: grid;
    gap: 0.75rem;
    grid-template-columns: minmax(14rem, 1.2fr) minmax(9rem, 0.8fr) minmax(9rem, 0.8fr) minmax(7rem, 0.5fr) minmax(10rem, 0.8fr) minmax(5rem, 0.4fr);
    min-width: 58rem;
    padding: 0.7rem 0.8rem;
  }

  .routes-head {
    background: transparent;
    border-color: transparent;
    font-size: 0.8rem;
    font-weight: 800;
    text-transform: uppercase;
  }

  .mini-switch span {
    border: 1px solid rgba(145, 181, 221, 0.16);
    border-radius: 999px;
    cursor: pointer;
    display: inline-flex;
    font-size: 0.84rem;
    font-weight: 800;
    justify-content: center;
    min-width: 6rem;
    padding: 0.4rem 0.65rem;
  }

  .mini-switch input:checked + span {
    background: rgba(77, 211, 168, 0.18);
    color: #8cf0cf;
  }

  .danger {
    color: #ffb0b0;
  }

  .stats-panel {
    display: grid;
    gap: 1rem;
    align-content: start;
  }

  dl {
    display: grid;
    gap: 0.65rem;
    margin: 0.85rem 0 0;
  }

  dl div {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
  }

  dt {
    color: var(--muted);
  }

  dd {
    font-weight: 800;
    margin: 0;
  }

  .decision-list {
    display: grid;
    gap: 0.6rem;
    margin-top: 0.85rem;
  }

  .decision-list article {
    border: 1px solid rgba(145, 181, 221, 0.1);
    border-radius: 8px;
    display: grid;
    gap: 0.2rem;
    padding: 0.65rem;
  }

  .decision-list time {
    font-size: 0.82rem;
  }

  .empty-state {
    margin: 0;
  }

  @media (max-width: 900px) {
    .router-grid {
      grid-template-columns: minmax(0, 1fr);
    }

    .router-header,
    .live-toggle-panel {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
