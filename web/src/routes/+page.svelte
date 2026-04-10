<script lang="ts">
  import { onDestroy, onMount } from "svelte";

  import {
    chartModeLabel,
    cycleChartMode,
    normalizeChartMode,
    readStoredChartMode,
    resolveChartModeCommand,
    writeStoredChartMode,
    type ChartMode,
  } from "$lib/chart-mode";
  import {
    filterPaletteItems,
    resolvePaletteQuery,
    splitSlashInput,
    type CommandPaletteItem,
  } from "$lib/command-palette";
  import {
    DaemonClient,
    DEFAULT_SESSION_NAME,
  } from "$lib/daemon/client";
  import { readStoredToken, writeStoredToken } from "$lib/daemon/storage";
  import type {
    CandleBar,
    ChatHistoryEntry,
    PortfolioSnapshot,
    ScheduledJobEnvelope,
    ServerEnvelope,
    SessionSummary,
    WatchlistQuote,
  } from "$lib/daemon/types";
  import ChartPanel from "$lib/components/ChartPanel.svelte";
  import ChatPanel from "$lib/components/ChatPanel.svelte";
  import CommandPalette from "$lib/components/CommandPalette.svelte";
  import EventPanel, { type EventRow } from "$lib/components/EventPanel.svelte";
  import PositionsPanel from "$lib/components/PositionsPanel.svelte";
  import WatchlistPanel from "$lib/components/WatchlistPanel.svelte";

  const client = new DaemonClient();
  const localhostHosts = new Set(["localhost", "127.0.0.1", "::1"]);

  let token = $state("");
  let sessionName = $state(DEFAULT_SESSION_NAME);
  let knownSessions = $state<SessionSummary[]>([]);
  let connectionStatus = $state("checking daemon...");
  let attachError = $state("");
  let isConnecting = $state(false);
  let tokenRequired = $state(false);
  let activeSession = $state("");
  let currentStatus = $state("idle");
  let queueDepth = $state(0);
  let watchlist = $state<string[]>([]);
  let snapshotSummary = $state("");
  let chartMode = $state<ChartMode>("full");
  let lastVisibleChartMode = $state<Exclude<ChartMode, "hide">>("full");
  let chartSymbol = $state("BTC");
  let chartTimeframe = $state("1m");
  let chartSource = $state("kai-api");
  let chatMessages = $state<ChatHistoryEntry[]>([]);
  let streamingReply = $state("");
  let chartQuote = $state<WatchlistQuote | null>(null);
  let watchlistQuotes = $state<WatchlistQuote[]>([]);
  let portfolio = $state<PortfolioSnapshot>({ positions: [], pnl: {} });
  let chartBars = $state<CandleBar[]>([]);
  let chartStatus = $state("waiting for a session");
  let alerts = $state<EventRow[]>([]);
  let natsEvents = $state<EventRow[]>([]);
  let schedulerEvents = $state<EventRow[]>([]);
  let inputDraft = $state("");
  let paletteOpen = $state(false);
  let paletteQuery = $state("");
  let paletteItems = $state<CommandPaletteItem[]>(filterPaletteItems(""));
  let pollingHandle: number | null = null;
  let daemonConnection = $state<Awaited<ReturnType<DaemonClient["attach"]>> | null>(null);

  function isScheduledJobEnvelope(envelope: ServerEnvelope): envelope is ScheduledJobEnvelope {
    return envelope.type.startsWith("scheduled_job_");
  }

  function pushRow(
    items: EventRow[],
    next: EventRow,
    limit = 8,
  ): EventRow[] {
    return [next, ...items].slice(0, limit);
  }

  function stopPolling(): void {
    if (pollingHandle !== null) {
      window.clearInterval(pollingHandle);
      pollingHandle = null;
    }
  }

  function updatePaletteItems(): void {
    paletteItems = filterPaletteItems(paletteQuery);
  }

  function openPalette(): void {
    paletteOpen = true;
    paletteQuery = "";
    updatePaletteItems();
  }

  function closePalette(): void {
    paletteOpen = false;
    paletteQuery = "";
    updatePaletteItems();
  }

  function applyChartMode(nextMode: ChartMode): void {
    if (nextMode !== "hide") {
      lastVisibleChartMode = nextMode;
    }
    chartMode = nextMode;
    if (daemonConnection) {
      daemonConnection.snapshot.chart_layout_mode = nextMode;
      writeStoredChartMode(daemonConnection.session, nextMode);
    }
    if (snapshotSummary) {
      snapshotSummary = `${chartSymbol} ${chartTimeframe} · ${chartSource} · chart ${chartMode} · ${chatMessages.length} chat messages`;
    }
  }

  function restoreChartMode(rawMode: string): void {
    const persistedMode =
      daemonConnection ? readStoredChartMode(daemonConnection.session) : null;
    chartMode = persistedMode ?? normalizeChartMode(rawMode);
    if (chartMode !== "hide") {
      lastVisibleChartMode = chartMode;
    }
    if (daemonConnection) {
      daemonConnection.snapshot.chart_layout_mode = chartMode;
    }
  }

  function formatPrice(value: number | undefined): string {
    if (typeof value !== "number" || Number.isNaN(value)) {
      return "price n/a";
    }
    if (Math.abs(value) >= 1000) {
      return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    }
    if (Math.abs(value) >= 1) {
      return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
    }
    return `$${value.toPrecision(4)}`;
  }

  function formatChange(value: number | undefined): string {
    if (typeof value !== "number" || Number.isNaN(value)) {
      return "24h n/a";
    }
    const sign = value > 0 ? "+" : "";
    return `${sign}${value.toFixed(2)}%`;
  }

  function chartPrice(): number | undefined {
    return chartQuote?.price ?? chartBars.at(-1)?.close;
  }

  function setChartModeFromCommand(raw: string): boolean {
    const split = splitSlashInput(raw);
    const nextMode = resolveChartModeCommand(split.command, split.args);
    if (!nextMode) {
      return false;
    }
    applyChartMode(nextMode);
    return true;
  }

  async function refreshChartData(): Promise<void> {
    if (!daemonConnection) {
      chartBars = [];
      chartStatus = "waiting for a session";
      return;
    }
    try {
      chartBars = await client.fetchChartHistory({
        symbol: chartSymbol,
        interval: chartTimeframe,
        source: chartSource,
        token,
      });
      chartStatus = `${chartBars.length} bars refreshed from ${chartSource}`;
    } catch (error) {
      chartStatus = error instanceof Error ? error.message : String(error);
    }
  }

  async function refreshSidebarData(): Promise<void> {
    if (!daemonConnection) {
      return;
    }
    const [quotes, chartQuotes, portfolioSnapshot] = await Promise.all([
      client.fetchWatchlistQuotes(watchlist, token),
      client.fetchWatchlistQuotes([chartSymbol], token),
      client.fetchPortfolio(token),
    ]);
    watchlistQuotes = quotes;
    chartQuote = chartQuotes[0] ?? null;
    portfolio = portfolioSnapshot;
  }

  function startPolling(): void {
    stopPolling();
    if (typeof window === "undefined") {
      return;
    }
    pollingHandle = window.setInterval(() => {
      void Promise.all([refreshSidebarData(), refreshChartData()]);
    }, 15_000);
  }

  function schedulerSummary(envelope: ScheduledJobEnvelope): EventRow {
    const headline = envelope.type.replace("scheduled_job_", "").replaceAll("_", " ");
    if ("job_id" in envelope) {
      const tail =
        "result_preview" in envelope && envelope.result_preview
          ? envelope.result_preview
          : "error" in envelope
            ? envelope.error
            : "job update";
      return {
        headline: `${headline} · ${envelope.job_id}`,
        detail: tail,
        tone: envelope.type.includes("failed") ? "danger" : "warning",
      };
    }
    return {
      headline: `${headline} · ${String(envelope.job?.id ?? "job")}`,
      detail: String(envelope.job?.status ?? "created"),
      tone: "warning",
    };
  }

  function applyEnvelope(envelope: ServerEnvelope): void {
    if (envelope.type === "session_attached") {
      applySnapshot();
      void Promise.all([refreshSidebarData(), refreshChartData()]);
      return;
    }

    if (envelope.type === "status") {
      currentStatus = envelope.activity;
      queueDepth = envelope.queue;
      return;
    }

    if (envelope.type === "token") {
      streamingReply += envelope.text;
      return;
    }

    if (envelope.type === "final") {
      chatMessages = [...chatMessages, { role: "ai", content: envelope.text }];
      streamingReply = "";
      return;
    }

    if (envelope.type === "signal") {
      const symbol = String(envelope.signal.symbol ?? "?");
      const side = String(envelope.signal.side ?? envelope.signal.direction ?? "signal");
      const score = envelope.signal.score;
      alerts = pushRow(alerts, {
        headline: `${symbol} ${side.toUpperCase()}`,
        detail: typeof score === "number" ? `score ${score.toFixed(2)}` : "signal received",
        tone: "positive",
      });
      return;
    }

    if (envelope.type === "nats_event") {
      natsEvents = pushRow(natsEvents, {
        headline: `${envelope.direction.toUpperCase()} ${envelope.subject}`,
        detail: JSON.stringify(envelope.payload).slice(0, 140),
      });
      return;
    }

    if (isScheduledJobEnvelope(envelope)) {
      schedulerEvents = pushRow(schedulerEvents, schedulerSummary(envelope));
      return;
    }

    if (envelope.type === "error") {
      attachError = envelope.message;
      alerts = pushRow(alerts, {
        headline: envelope.code,
        detail: envelope.message,
        tone: "danger",
      });
      return;
    }
  }

  function applySnapshot(): void {
    if (!daemonConnection) {
      snapshotSummary = "";
      chartMode = "full";
      lastVisibleChartMode = "full";
      chartSymbol = "BTC";
      chartTimeframe = "1m";
      chartSource = "kai-api";
      chartQuote = null;
      watchlist = [];
      chatMessages = [];
      return;
    }
    const snapshot = daemonConnection.snapshot;
    chartSymbol = snapshot.chart_symbol;
    chartTimeframe = snapshot.chart_timeframe;
    chartSource = snapshot.chart_source;
    restoreChartMode(snapshot.chart_layout_mode);
    watchlist = snapshot.watchlist_symbols;
    snapshotSummary = `${chartSymbol} ${chartTimeframe} · ${chartSource} · chart ${chartMode} · ${snapshot.chat_history.length} chat messages`;
    chatMessages = [...snapshot.chat_history];
  }

  async function refreshSessions(): Promise<void> {
    try {
      knownSessions = await client.listSessions(token);
      if (!knownSessions.length) {
        connectionStatus = "daemon reachable; no sessions yet";
      } else if (!daemonConnection) {
        connectionStatus = `daemon reachable; ${knownSessions.length} session${knownSessions.length === 1 ? "" : "s"} available`;
      }
    } catch (error) {
      connectionStatus = "daemon session list unavailable";
      attachError = error instanceof Error ? error.message : String(error);
    }
  }

  async function attachSession(): Promise<void> {
    attachError = "";
    isConnecting = true;
    writeStoredToken(token);
    try {
      if (daemonConnection) {
        daemonConnection.onClose = undefined;
        daemonConnection.close(1000, "reconnect");
      }
      daemonConnection = await client.attach({
        session: sessionName,
        token,
        createIfMissing: true,
      });
      daemonConnection.onEnvelope = applyEnvelope;
      daemonConnection.onClose = (code) => {
        connectionStatus = `daemon disconnected (${code ?? 1000})`;
        activeSession = "";
        daemonConnection = null;
        chartQuote = null;
        stopPolling();
      };
      daemonConnection.subscribe("signals");
      daemonConnection.subscribe("nats");
      activeSession = daemonConnection.session;
      currentStatus = daemonConnection.activityStatus;
      queueDepth = daemonConnection.queueDepth;
      connectionStatus = `attached to session ${activeSession}`;
      applySnapshot();
      await refreshSessions();
      await Promise.all([refreshSidebarData(), refreshChartData()]);
      startPolling();
    } catch (error) {
      attachError = error instanceof Error ? error.message : String(error);
      connectionStatus = "attach failed";
      daemonConnection = null;
      activeSession = "";
    } finally {
      isConnecting = false;
    }
  }

  function disconnectSession(): void {
    daemonConnection?.close(1000, "manual disconnect");
    daemonConnection = null;
    activeSession = "";
    connectionStatus = "disconnected";
    snapshotSummary = "";
    chartMode = "full";
    lastVisibleChartMode = "full";
    chartSymbol = "BTC";
    chartTimeframe = "1m";
    chartSource = "kai-api";
    chartQuote = null;
    watchlist = [];
    watchlistQuotes = [];
    portfolio = { positions: [], pnl: {} };
    chartBars = [];
    chartStatus = "waiting for a session";
    alerts = [];
    natsEvents = [];
    schedulerEvents = [];
    chatMessages = [];
    streamingReply = "";
    stopPolling();
  }

  function onConnectSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void attachSession();
  }

  function sendMessage(): void {
    if (!daemonConnection || !inputDraft.trim()) {
      return;
    }
    const text = inputDraft.trim();
    inputDraft = "";
    if (setChartModeFromCommand(text)) {
      return;
    }
    chatMessages = [...chatMessages, { role: "human", content: text }];
    streamingReply = "";
    if (text.startsWith("/")) {
      const firstSpace = text.indexOf(" ");
      const command = firstSpace === -1 ? text : text.slice(0, firstSpace);
      const args = firstSpace === -1 ? "" : text.slice(firstSpace + 1);
      daemonConnection.sendSlash(command, args);
      return;
    }
    daemonConnection.sendInput(text);
  }

  function onInputSubmit(event: SubmitEvent): void {
    event.preventDefault();
    sendMessage();
  }

  function onInputKeydown(event: KeyboardEvent): void {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    event.preventDefault();
    sendMessage();
  }

  function executePaletteCommand(raw: string): void {
    if (!daemonConnection) {
      return;
    }
    const resolved = resolvePaletteQuery(raw, paletteItems);
    if (setChartModeFromCommand(resolved)) {
      closePalette();
      return;
    }
    const split = splitSlashInput(resolved);
    if (!split.command) {
      return;
    }
    chatMessages = [
      ...chatMessages,
      {
        role: "human",
        content: `${split.command}${split.args ? ` ${split.args}` : ""}`,
      },
    ];
    streamingReply = "";
    daemonConnection.sendSlash(split.command, split.args);
    closePalette();
  }

  onMount(() => {
    tokenRequired = !localhostHosts.has(window.location.hostname);
    token = tokenRequired ? readStoredToken() : "";
    void refreshSessions();
    const onKeydown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        void openPalette();
      } else if (paletteOpen && event.key === "Escape") {
        event.preventDefault();
        closePalette();
      } else if (paletteOpen && event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        executePaletteCommand(paletteQuery);
      }
    };
    window.addEventListener("keydown", onKeydown);
    return () => {
      window.removeEventListener("keydown", onKeydown);
    };
  });

  onDestroy(() => {
    stopPolling();
    daemonConnection?.close(1000, "page teardown");
  });

  $effect(() => {
    if (typeof document === "undefined") {
      return;
    }
    const connected = Boolean(daemonConnection);
    document.body.classList.toggle("dashboard-active", connected);
    return () => {
      document.body.classList.remove("dashboard-active");
    };
  });
</script>

<svelte:head>
  <title>KAI Web Terminal</title>
  <meta
    name="description"
    content="Browser client for the daemon-backed KAI trading terminal."
  />
</svelte:head>

<section class="landing-shell" class:dashboard-mode={Boolean(daemonConnection)}>
  {#if daemonConnection}
    <div class="dashboard-shell">
      <header class="dashboard-topbar">
        <div class="dashboard-heading">
          <div class="dashboard-brand">
            <p class="eyebrow">KAI</p>
            <strong>Web Terminal</strong>
          </div>
          <div class="dashboard-meta status-strip">
            <span>session: <strong>{activeSession}</strong></span>
            <span>status: <strong>{currentStatus}</strong></span>
            <span>queue: <strong>{queueDepth}</strong></span>
            <span class="dashboard-chart-pill">
              chart:
              <strong>
                {chartSymbol} {chartTimeframe} [{chartMode}]
              </strong>
              <button
                aria-label="Cycle chart size"
                class="chart-mode-toggle"
                onclick={() => applyChartMode(cycleChartMode(chartMode))}
                type="button"
              >
                {chartModeLabel(chartMode)}
              </button>
            </span>
            <span>positions: <strong>{portfolio.positions.length}</strong></span>
            <span>watchlist: <strong>{watchlist.length}</strong></span>
          </div>
        </div>

        <div class="dashboard-actions">
          <button onclick={disconnectSession} type="button">Disconnect</button>
          <button class="secondary" onclick={openPalette} type="button">Ctrl+K</button>
        </div>
      </header>

      {#if snapshotSummary}
        <p class="dashboard-summary">{snapshotSummary}</p>
      {/if}

      {#if attachError}
        <p class="dashboard-error">{attachError}</p>
      {/if}

      <div class="dashboard-grid">
        <div class="dashboard-column left">
          <WatchlistPanel initiallyOpen={false} mobileCollapsible={true} quotes={watchlistQuotes} />
          <PositionsPanel initiallyOpen={false} mobileCollapsible={true} {portfolio} />
        </div>

        <div class="dashboard-column center" data-chart-mode={chartMode}>
          {#if chartMode === "hide"}
            <section class="chart-status-bar">
              <div class="chart-status-copy">
                <span>{chartSymbol}</span>
                <strong>{formatPrice(chartPrice())}</strong>
                <span
                  class:negative={Boolean(chartQuote && typeof chartQuote.price_change_24h_pct === "number" && chartQuote.price_change_24h_pct < 0)}
                  class:positive={Boolean(chartQuote && typeof chartQuote.price_change_24h_pct === "number" && chartQuote.price_change_24h_pct > 0)}
                >
                  {formatChange(chartQuote?.price_change_24h_pct)}
                </span>
              </div>
              <button onclick={() => applyChartMode(lastVisibleChartMode)} type="button">
                Show Chart
              </button>
            </section>
          {:else}
            <ChartPanel
              bars={chartBars}
              initiallyOpen={false}
              mobileCollapsible={true}
              mode={chartMode}
              source={chartSource}
              status={chartStatus}
              symbol={chartSymbol}
              timeframe={chartTimeframe}
            />
          {/if}

          <ChatPanel
            initiallyOpen={false}
            messages={chatMessages}
            mobileCollapsible={true}
            {streamingReply}
          />

          <form class="chat-input" onsubmit={onInputSubmit}>
            <textarea
              bind:value={inputDraft}
              onkeydown={onInputKeydown}
              placeholder="Type a prompt or slash command for this session"
              rows="3"
            ></textarea>
            <button type="submit">Send</button>
          </form>
        </div>

        <div class="dashboard-column right">
          <EventPanel
            eyebrow="Signals"
            emptyMessage="No alert envelopes yet."
            initiallyOpen={false}
            items={alerts}
            mobileCollapsible={true}
            subtitle={`${alerts.length} recent`}
            title="Alerts"
          />
          <EventPanel
            eyebrow="Bus"
            emptyMessage="No NATS traffic has hit this session yet."
            initiallyOpen={false}
            items={natsEvents}
            mobileCollapsible={true}
            subtitle={`${natsEvents.length} recent`}
            title="NATS"
          />
          <EventPanel
            eyebrow="Scheduler"
            emptyMessage="No scheduler activity yet."
            initiallyOpen={false}
            items={schedulerEvents}
            mobileCollapsible={true}
            subtitle={`${schedulerEvents.length} recent`}
            title="Scheduled Jobs"
          />
        </div>
      </div>
    </div>
  {:else}
    <div class="landing-card">
      <p class="eyebrow">Daemon Client</p>
      <h1>KAI Web Terminal</h1>
      <p class="summary">
        The browser client attaches to the same daemon sessions as the terminal and
        reuses the daemon websocket protocol directly. The web dashboard now mirrors
        the terminal layout with live watchlist, positions, alerts, NATS traffic,
        scheduler events, and a raw chat stream, while keeping the chart panel ready
        for the dedicated `P6.5` Lightweight Charts integration.
      </p>

      <div class="status-banner">
        <span>{connectionStatus}</span>
      </div>

      <div class="shortcut-hint">
        <span>Ctrl+K</span>
        <p>Open the slash command palette and execute daemon-side commands without leaving the dashboard.</p>
      </div>

      <div class="connect-grid">
        <form class="connect-panel" onsubmit={onConnectSubmit}>
          <label>
            <span>Session</span>
            <input
              bind:value={sessionName}
              list="known-sessions"
              name="session"
              placeholder="terminal"
              required
              type="text"
            />
          </label>

          <label>
            <span>Daemon token</span>
            <input
              bind:value={token}
              autocomplete="off"
              name="token"
              placeholder="Paste bearer token if required"
              required={tokenRequired}
              type="password"
            />
          </label>

          <datalist id="known-sessions">
            {#each knownSessions as session (session.name)}
              <option value={session.name}></option>
            {/each}
          </datalist>

          <div class="button-row">
            <button disabled={isConnecting} type="submit">
              {#if isConnecting}Attaching...{:else}Attach Session{/if}
            </button>
            <button disabled={isConnecting} onclick={() => void refreshSessions()} type="button">
              Refresh
            </button>
          </div>

          <p class="token-hint">
            Remote or proxied clients should use the daemon bearer token from
            <code>workspaces/daemon-token.txt</code>. Direct localhost sessions can
            still attach without one.
          </p>

          {#if attachError}
            <p class="error-text">{attachError}</p>
          {/if}
        </form>

        <div class="session-panel">
          <h2>Known Sessions</h2>
          {#if knownSessions.length}
            <ul class="session-list">
              {#each knownSessions as session (session.name)}
                <li>
                  <strong>{session.name}</strong>
                  <span>{session.activity_status ?? "idle"}</span>
                </li>
              {/each}
            </ul>
          {:else}
            <p>No session attached yet.</p>
          {/if}
        </div>
      </div>

      <dl class="checklist">
        <div>
          <dt>Transport</dt>
          <dd>WebSocket session attach plus REST snapshots for sidebar panels.</dd>
        </div>
        <div>
          <dt>Layout</dt>
          <dd>Left rail for market state, center stack for chart and chat, right rail for events.</dd>
        </div>
        <div>
          <dt>Next Slice</dt>
          <dd>Replace the chart placeholder with Lightweight Charts and live candles.</dd>
        </div>
      </dl>
    </div>
  {/if}
</section>

<CommandPalette
  activeSession={activeSession}
  items={paletteItems}
  onClose={closePalette}
  onQueryChange={(value) => {
    paletteQuery = value;
    updatePaletteItems();
  }}
  onSelect={executePaletteCommand}
  open={paletteOpen}
  query={paletteQuery}
/>
