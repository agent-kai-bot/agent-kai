<script lang="ts">
  import { onDestroy, onMount } from "svelte";

  import {
    normalizeChartMode,
    readStoredChartMode,
    resolveChartCommandInput,
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
  import {
    buildSymbolSuggestions,
    normalizeMarketSymbol,
    normalizeSignalAlert,
    signalChartPatch,
    signalCountsBySymbol,
    type SignalAlert,
  } from "$lib/market-ui";
  import type {
    CandleBar,
    ChatHistoryEntry,
    ChartViewPatch,
    ChartViewState,
    EndpointModelSummary,
    ModelAgentSummary,
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
  import SignalPanel from "$lib/components/SignalPanel.svelte";
  import WatchlistPanel from "$lib/components/WatchlistPanel.svelte";

  const client = new DaemonClient();
  const localhostHosts = new Set(["localhost", "127.0.0.1", "::1"]);
  const reasoningChoices = ["none", "minimal", "low", "medium", "high", "xhigh"];
  const chartPanePresets: Record<Exclude<ChartMode, "hide">, number> = {
    full: 56,
    half: 50,
    mini: 34,
  };

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
  let symbolSearch = $state("");
  let symbolSearchOpen = $state(false);
  let activeSuggestionIndex = $state(0);
  let chatMessages = $state<ChatHistoryEntry[]>([]);
  let streamingReply = $state("");
  let chartQuote = $state<WatchlistQuote | null>(null);
  let watchlistQuotes = $state<WatchlistQuote[]>([]);
  let portfolio = $state<PortfolioSnapshot>({ positions: [], pnl: {} });
  let chartBars = $state<CandleBar[]>([]);
  let chartStatus = $state("waiting for a session");
  let signalAlerts = $state<SignalAlert[]>([]);
  let selectedSignalId = $state("");
  let natsEvents = $state<EventRow[]>([]);
  let schedulerEvents = $state<EventRow[]>([]);
  let inputDraft = $state("");
  let paletteOpen = $state(false);
  let paletteQuery = $state("");
  let paletteItems = $state<CommandPaletteItem[]>(filterPaletteItems(""));
  let pollingHandle: number | null = null;
  let daemonConnection = $state<Awaited<ReturnType<DaemonClient["attach"]>> | null>(null);
  let modelAgents = $state<ModelAgentSummary[]>([]);
  let modelEndpoints = $state<EndpointModelSummary[]>([]);
  let selectedModelAgent = $state("kai");
  let selectedModelRef = $state("");
  let selectedReasoningEffort = $state("medium");
  let modelStatus = $state("model info unavailable");
  let isSwitchingModel = $state(false);
  let isStoppingStream = $state(false);
  let isUpdatingChart = $state(false);
  let chartUpdateError = $state("");
  let chartUpdateNotice = $state("");
  let lastChartUpdateMs = $state<number | null>(null);
  let streamStartedAt = $state<number | null>(null);
  let firstTokenAt = $state<number | null>(null);
  let streamChunkCount = $state(0);
  let streamCharacterCount = $state(0);
  let leftPanePct = $state(20);
  let rightPanePct = $state(20);
  let chartPanePct = $state(56);

  function isScheduledJobEnvelope(envelope: ServerEnvelope): envelope is ScheduledJobEnvelope {
    return envelope.type.startsWith("scheduled_job_");
  }

  function pushRow<T>(
    items: T[],
    next: T,
    limit = 8,
  ): T[] {
    return [next, ...items].slice(0, limit);
  }

  function nowMs(): number {
    return typeof performance === "undefined" ? Date.now() : performance.now();
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
      chartPanePct = chartPanePresets[nextMode];
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

  function applyChartViewState(chartView: ChartViewState): void {
    const previousSymbol = chartSymbol;
    const previousTimeframe = chartTimeframe;
    const previousSource = chartSource;
    chartSymbol = chartView.chart_symbol;
    chartTimeframe = chartView.chart_timeframe;
    chartSource = chartView.chart_source;
    syncSymbolSearch();
    applyChartMode(normalizeChartMode(chartView.chart_layout_mode));
    if (daemonConnection) {
      daemonConnection.snapshot.chart_symbol = chartSymbol;
      daemonConnection.snapshot.chart_timeframe = chartTimeframe;
      daemonConnection.snapshot.chart_source = chartSource;
      daemonConnection.snapshot.chart_layout_mode = chartMode;
    }
    const changedMarket =
      previousSymbol !== chartSymbol ||
      previousTimeframe !== chartTimeframe ||
      previousSource !== chartSource;
    if (changedMarket) {
      void Promise.all([refreshSidebarData(), refreshChartData()]);
    }
  }

  async function requestChartViewUpdate(patch: ChartViewPatch): Promise<void> {
    if (!daemonConnection) {
      return;
    }
    const startedAt = nowMs();
    isUpdatingChart = true;
    chartUpdateError = "";
    chartUpdateNotice = "";
    try {
      const response = await client.updateChartView(
        daemonConnection.session,
        patch,
        token,
      );
      applyChartViewState(response.chart);
      lastChartUpdateMs = Math.round(nowMs() - startedAt);
      chartUpdateNotice = `Updated ${response.chart.chart_symbol} ${response.chart.chart_timeframe}`;
    } catch (error) {
      chartUpdateError = error instanceof Error ? error.message : String(error);
    } finally {
      isUpdatingChart = false;
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

  function symbolSuggestions() {
    return buildSymbolSuggestions({
      activeSymbol: chartSymbol,
      watchlist,
      quotes: watchlistQuotes,
      signals: signalAlerts,
      query: symbolSearch,
    });
  }

  function syncSymbolSearch(): void {
    symbolSearch = chartSymbol;
    activeSuggestionIndex = 0;
  }

  function signalCounts(): Record<string, number> {
    return signalCountsBySymbol(signalAlerts);
  }

  function chartUpdateLabel(): string {
    if (isUpdatingChart) {
      return "Updating chart";
    }
    if (chartUpdateError) {
      return chartUpdateError;
    }
    if (chartUpdateNotice) {
      return lastChartUpdateMs === null
        ? chartUpdateNotice
        : `${chartUpdateNotice} in ${lastChartUpdateMs}ms`;
    }
    return "Ready";
  }

  function streamLatencyLabel(): string {
    if (firstTokenAt !== null && streamStartedAt !== null) {
      return `${Math.round(firstTokenAt - streamStartedAt)}ms first token`;
    }
    if (streamStartedAt !== null && currentStatus !== "idle") {
      return "waiting for first token";
    }
    return "stream idle";
  }

  function streamThroughputLabel(): string {
    if (
      firstTokenAt === null ||
      streamCharacterCount === 0 ||
      streamStartedAt === null
    ) {
      return "0 chunks";
    }
    const elapsedSeconds = Math.max((nowMs() - firstTokenAt) / 1000, 0.1);
    const charsPerSecond = Math.round(streamCharacterCount / elapsedSeconds);
    return `${streamChunkCount} chunks · ${charsPerSecond} chars/s`;
  }

  function chartAnalysisPrompts(): Array<{ label: string; prompt: string }> {
    const context = `${chartSymbol} ${chartTimeframe} chart from ${chartSource}`;
    return [
      {
        label: "Wyckoff + Fib",
        prompt: (
          `Analyze the currently visible ${context} using Wyckoff market ` +
          "structure and Fibonacci retracement/extension levels. Cover the " +
          "likely phase, key support/resistance, invalidation level, upside " +
          "and downside scenarios, confidence, and whether the best action is " +
          "wait, enter, reduce, or avoid."
        ),
      },
      {
        label: "Trend Read",
        prompt: (
          `Analyze the currently visible ${context} as a trend-following setup. ` +
          "Focus on trend direction, market structure, moving momentum, volume " +
          "confirmation, pullback zones, breakout risk, and the cleanest entry " +
          "and invalidation plan."
        ),
      },
      {
        label: "Risk Setup",
        prompt: (
          `Analyze the currently visible ${context} for a trade decision. ` +
          "Summarize bullish and bearish evidence, define entry zones, stop " +
          "placement, targets, reward/risk, position-sizing considerations, " +
          "and conditions that would make the setup invalid."
        ),
      },
    ];
  }

  function resetStreamMetrics(): void {
    streamStartedAt = nowMs();
    firstTokenAt = null;
    streamChunkCount = 0;
    streamCharacterCount = 0;
  }

  function recordStreamToken(text: string): void {
    const now = nowMs();
    if (streamStartedAt === null) {
      streamStartedAt = now;
    }
    if (firstTokenAt === null) {
      firstTokenAt = now;
    }
    streamChunkCount += 1;
    streamCharacterCount += text.length;
  }

  function clamp(value: number, minimum: number, maximum: number): number {
    return Math.min(Math.max(value, minimum), maximum);
  }

  function dashboardGridStyle(): string {
    return `--left-pane: ${leftPanePct}%; --right-pane: ${rightPanePct}%;`;
  }

  function centerColumnStyle(): string {
    return `--chart-pane: ${chartPanePct}%;`;
  }

  function resizeColumnPane(event: PointerEvent, pane: "left" | "right"): void {
    const grid = (event.currentTarget as HTMLElement).parentElement;
    if (!grid) {
      return;
    }
    const bounds = grid.getBoundingClientRect();
    const pointerId = event.pointerId;
    (event.currentTarget as HTMLElement).setPointerCapture(pointerId);
    const onMove = (moveEvent: PointerEvent) => {
      const relativeX = clamp(moveEvent.clientX - bounds.left, 0, bounds.width);
      if (pane === "left") {
        leftPanePct = clamp((relativeX / bounds.width) * 100, 12, 38);
      } else {
        rightPanePct = clamp(
          ((bounds.width - relativeX) / bounds.width) * 100,
          12,
          38,
        );
      }
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
  }

  function resizeChartPane(event: PointerEvent): void {
    const column = (event.currentTarget as HTMLElement).parentElement;
    if (!column) {
      return;
    }
    const bounds = column.getBoundingClientRect();
    const pointerId = event.pointerId;
    (event.currentTarget as HTMLElement).setPointerCapture(pointerId);
    const onMove = (moveEvent: PointerEvent) => {
      const relativeY = clamp(moveEvent.clientY - bounds.top, 0, bounds.height);
      chartPanePct = clamp((relativeY / bounds.height) * 100, 24, 74);
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
  }

  async function resetChartSplit(): Promise<void> {
    await requestChartViewUpdate({ mode: "full" });
  }

  async function selectChartSymbol(symbol: string): Promise<void> {
    const target = normalizeMarketSymbol(symbol);
    if (!target) {
      return;
    }
    chartUpdateError = "";
    const existingQuote = watchlistQuotes.find((quote) => quote.symbol === target);
    if (!existingQuote || existingQuote.error) {
      const [quote] = await client.fetchWatchlistQuotes([target], token);
      if (!quote || quote.error || typeof quote.price !== "number") {
        chartUpdateError = `${target} was not found`;
        symbolSearch = target;
        symbolSearchOpen = true;
        return;
      }
    }
    symbolSearchOpen = false;
    symbolSearch = target;
    await requestChartViewUpdate({ symbol: target });
  }

  async function selectSignalAlert(alert: SignalAlert): Promise<void> {
    selectedSignalId = alert.id;
    await requestChartViewUpdate(signalChartPatch(alert));
  }

  async function addWatchlistSymbol(symbol: string): Promise<void> {
    const target = normalizeMarketSymbol(symbol);
    if (!target || watchlist.includes(target)) {
      return;
    }
    if (!daemonConnection) {
      watchlist = [...watchlist, target];
      return;
    }
    const response = await client.updateSessionWatchlist(
      daemonConnection.session,
      { add: target },
      token,
    );
    watchlist = response.watchlist.watchlist_symbols;
    await refreshSidebarData();
  }

  async function removeWatchlistSymbol(symbol: string): Promise<void> {
    const target = normalizeMarketSymbol(symbol);
    if (daemonConnection) {
      const response = await client.updateSessionWatchlist(
        daemonConnection.session,
        { remove: target },
        token,
      );
      watchlist = response.watchlist.watchlist_symbols;
    } else {
      watchlist = watchlist.filter((item) => item !== target);
    }
    watchlistQuotes = watchlistQuotes.filter((quote) => quote.symbol !== target);
    await refreshSidebarData();
  }

  function chartSymbolIsWatched(): boolean {
    return watchlist.includes(chartSymbol);
  }

  async function toggleChartSymbolWatchlist(): Promise<void> {
    if (chartSymbolIsWatched()) {
      await removeWatchlistSymbol(chartSymbol);
      chartUpdateNotice = `Removed ${chartSymbol} from watchlist`;
      return;
    }
    await addWatchlistSymbol(chartSymbol);
    chartUpdateNotice = `Added ${chartSymbol} to watchlist`;
  }

  function onSymbolSelectorFocusOut(event: FocusEvent): void {
    const current = event.currentTarget as HTMLElement;
    const next = event.relatedTarget as Node | null;
    if (!next || !current.contains(next)) {
      symbolSearchOpen = false;
    }
  }

  function onSymbolSearchKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      symbolSearchOpen = false;
      syncSymbolSearch();
      return;
    }
    const suggestions = symbolSuggestions();
    if (event.key === "ArrowDown") {
      event.preventDefault();
      symbolSearchOpen = true;
      activeSuggestionIndex = Math.min(
        activeSuggestionIndex + 1,
        Math.max(suggestions.length - 1, 0),
      );
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      symbolSearchOpen = true;
      activeSuggestionIndex = Math.max(activeSuggestionIndex - 1, 0);
      return;
    }
    if (event.key !== "Enter") {
      return;
    }
    event.preventDefault();
    const activeSuggestion = suggestions[activeSuggestionIndex]?.symbol;
    const target = activeSuggestion ?? normalizeMarketSymbol(symbolSearch);
    if (target) {
      void selectChartSymbol(target);
    }
  }

  function modelRefFor(agent: ModelAgentSummary | undefined): string {
    if (!agent?.endpoint || !agent?.model) {
      return "";
    }
    return `${agent.endpoint}/${agent.model}`;
  }

  function selectedAgentSummary(): ModelAgentSummary | undefined {
    return modelAgents.find((agent) => agent.name === selectedModelAgent);
  }

  function selectedAgentLabel(): string {
    const agent = selectedAgentSummary();
    if (!agent) {
      return "unknown";
    }
    return `${agent.name}: ${agent.endpoint ?? "endpoint?"}/${agent.model ?? "model?"} · ${agent.reasoning_effort ?? "default"} thinking`;
  }

  function modelChoices(): Array<{ value: string; label: string }> {
    return modelEndpoints.flatMap((endpoint) =>
      endpoint.models.map((model) => ({
        value: `${endpoint.name}/${model}`,
        label: `${endpoint.name} / ${model}`,
      })),
    );
  }

  function syncSelectedModelRef(): void {
    const agent = selectedAgentSummary();
    const currentRef = modelRefFor(agent);
    selectedModelRef = currentRef || modelChoices()[0]?.value || "";
    selectedReasoningEffort = agent?.reasoning_effort || "medium";
  }

  function onModelAgentChange(): void {
    syncSelectedModelRef();
  }

  async function refreshModelInfo(): Promise<void> {
    try {
      const registry = await client.fetchModelRegistry(token);
      modelAgents = registry.agents;
      modelEndpoints = registry.endpoints;
      if (!modelAgents.some((agent) => agent.name === selectedModelAgent)) {
        selectedModelAgent = modelAgents[0]?.name ?? "kai";
      }
      syncSelectedModelRef();
      modelStatus = `model: ${selectedAgentLabel()}`;
    } catch (error) {
      modelStatus = error instanceof Error ? error.message : String(error);
    }
  }

  async function switchSelectedModel(): Promise<void> {
    if (!selectedModelAgent || !selectedModelRef || isSwitchingModel) {
      return;
    }
    const [endpoint, ...modelParts] = selectedModelRef.split("/");
    const model = modelParts.join("/");
    if (!endpoint || !model) {
      modelStatus = "select an endpoint and model";
      return;
    }
    isSwitchingModel = true;
    modelStatus = "switching model...";
    try {
      const result = await client.switchAgentModel(
        selectedModelAgent,
        endpoint,
        model,
        token,
        selectedReasoningEffort,
      );
      await refreshModelInfo();
      const reloadCount = result.reloaded_sessions.length;
      modelStatus = `model: ${result.agent.name}: ${endpoint}/${model} · ${result.agent.reasoning_effort ?? selectedReasoningEffort} thinking; reloaded ${reloadCount}`;
    } catch (error) {
      modelStatus = error instanceof Error ? error.message : String(error);
    } finally {
      isSwitchingModel = false;
    }
  }

  async function stopCurrentStream(): Promise<void> {
    if (!activeSession || isStoppingStream) {
      return;
    }
    isStoppingStream = true;
    try {
      await client.stopSession(activeSession, token);
      daemonConnection?.interrupt();
      streamingReply = "";
      currentStatus = "idle";
    } catch (error) {
      attachError = error instanceof Error ? error.message : String(error);
    } finally {
      isStoppingStream = false;
    }
  }

  async function setChartCommandFromInput(raw: string): Promise<boolean> {
    const split = splitSlashInput(raw);
    const command = resolveChartCommandInput(split.command, split.args);
    if (!command) {
      return false;
    }
    if (command.mode) {
      await requestChartViewUpdate({ mode: command.mode });
      return true;
    }
    await requestChartViewUpdate({
      symbol: command.symbol,
      timeframe: command.timeframe,
    });
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
      recordStreamToken(envelope.text);
      streamingReply += envelope.text;
      return;
    }

    if (envelope.type === "final") {
      chatMessages = [...chatMessages, { role: "ai", content: envelope.text }];
      streamingReply = "";
      return;
    }

    if (envelope.type === "chart_view") {
      applyChartViewState(envelope);
      return;
    }

    if (envelope.type === "watchlist") {
      watchlist = envelope.watchlist_symbols;
      void refreshSidebarData();
      return;
    }

    if (envelope.type === "signal") {
      const alert = normalizeSignalAlert(envelope.signal, signalAlerts.length);
      signalAlerts = pushRow(signalAlerts, alert, 20);
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
      syncSymbolSearch();
      chartQuote = null;
      watchlist = [];
      chatMessages = [];
      return;
    }
    const snapshot = daemonConnection.snapshot;
    chartSymbol = snapshot.chart_symbol;
    chartTimeframe = snapshot.chart_timeframe;
    chartSource = snapshot.chart_source;
    syncSymbolSearch();
    restoreChartMode(snapshot.chart_layout_mode);
    watchlist = snapshot.watchlist_symbols;
    const totalMessages = snapshot.chat_history_total ?? snapshot.chat_history.length;
    const recentLabel =
      snapshot.chat_history_omitted && snapshot.chat_history_omitted > 0
        ? `${snapshot.chat_history.length}/${totalMessages} recent chat messages`
        : `${totalMessages} chat messages`;
    snapshotSummary = `${chartSymbol} ${chartTimeframe} · ${chartSource} · chart ${chartMode} · ${recentLabel}`;
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
      await refreshModelInfo();
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
    signalAlerts = [];
    selectedSignalId = "";
    natsEvents = [];
    schedulerEvents = [];
    chatMessages = [];
    streamingReply = "";
    streamStartedAt = null;
    firstTokenAt = null;
    streamChunkCount = 0;
    streamCharacterCount = 0;
    modelStatus = modelAgents.length ? `model: ${selectedAgentLabel()}` : "model info unavailable";
    stopPolling();
  }

  function onConnectSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void attachSession();
  }

  async function sendMessage(): Promise<void> {
    if (!daemonConnection || !inputDraft.trim()) {
      return;
    }
    const text = inputDraft.trim();
    inputDraft = "";
    if (await setChartCommandFromInput(text)) {
      return;
    }
    chatMessages = [...chatMessages, { role: "human", content: text }];
    streamingReply = "";
    resetStreamMetrics();
    if (text.startsWith("/")) {
      const firstSpace = text.indexOf(" ");
      const command = firstSpace === -1 ? text : text.slice(0, firstSpace);
      const args = firstSpace === -1 ? "" : text.slice(firstSpace + 1);
      daemonConnection.sendSlash(command, args);
      return;
    }
    daemonConnection.sendInput(text);
  }

  async function sendPromptPreset(prompt: string): Promise<void> {
    if (!daemonConnection || !prompt.trim()) {
      return;
    }
    const text = prompt.trim();
    chatMessages = [...chatMessages, { role: "human", content: text }];
    streamingReply = "";
    resetStreamMetrics();
    daemonConnection.sendInput(text);
  }

  function onInputSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void sendMessage();
  }

  function onInputKeydown(event: KeyboardEvent): void {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    event.preventDefault();
    void sendMessage();
  }

  async function executePaletteCommand(raw: string): Promise<void> {
    if (!daemonConnection) {
      return;
    }
    const resolved = resolvePaletteQuery(raw, paletteItems);
    if (await setChartCommandFromInput(resolved)) {
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
    resetStreamMetrics();
    daemonConnection.sendSlash(split.command, split.args);
    closePalette();
  }

  onMount(() => {
    tokenRequired = !localhostHosts.has(window.location.hostname);
    token = tokenRequired ? readStoredToken() : "";
    void refreshSessions();
    void refreshModelInfo();
    const onKeydown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        void openPalette();
      } else if (paletteOpen && event.key === "Escape") {
        event.preventDefault();
        closePalette();
      } else if (paletteOpen && event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        void executePaletteCommand(paletteQuery);
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
            <span>model: <strong>{selectedAgentLabel()}</strong></span>
            <span>chart: <strong>{chartSymbol} {chartTimeframe}</strong></span>
            <span>positions: <strong>{portfolio.positions.length}</strong></span>
            <span>watchlist: <strong>{watchlist.length}</strong></span>
          </div>
        </div>

        <div class="dashboard-actions">
          <div class="model-picker">
            <select
              aria-label="Agent"
              bind:value={selectedModelAgent}
              onchange={onModelAgentChange}
            >
              {#each modelAgents as agent (agent.name)}
                <option value={agent.name}>{agent.name}</option>
              {/each}
            </select>
            <select aria-label="Model" bind:value={selectedModelRef}>
              {#each modelChoices() as choice (choice.value)}
                <option value={choice.value}>{choice.label}</option>
              {/each}
            </select>
            <select aria-label="Thinking level" bind:value={selectedReasoningEffort}>
              {#each reasoningChoices as effort}
                <option value={effort}>{effort} thinking</option>
              {/each}
            </select>
            <button
              class="secondary"
              disabled={isSwitchingModel || !selectedModelRef}
              onclick={() => void switchSelectedModel()}
              type="button"
            >
              {#if isSwitchingModel}Switching...{:else}Switch{/if}
            </button>
          </div>
          <button
            class="danger"
            disabled={isStoppingStream || currentStatus === "idle"}
            onclick={() => void stopCurrentStream()}
            type="button"
          >
            {#if isStoppingStream}Stopping...{:else}Stop{/if}
          </button>
          <button onclick={disconnectSession} type="button">Disconnect</button>
          <button class="secondary" onclick={openPalette} type="button">Ctrl+K</button>
        </div>
      </header>

      <p class="model-status">{modelStatus}</p>

      {#if snapshotSummary}
        <p class="dashboard-summary">{snapshotSummary}</p>
      {/if}

      {#if attachError}
        <p class="dashboard-error">{attachError}</p>
      {/if}

      <div class="dashboard-grid" style={dashboardGridStyle()}>
        <div class="dashboard-column left">
          <WatchlistPanel
            activeSymbol={chartSymbol}
            initiallyOpen={false}
            mobileCollapsible={true}
            onAddSymbol={(symbol) => void addWatchlistSymbol(symbol)}
            onRemoveSymbol={(symbol) => void removeWatchlistSymbol(symbol)}
            onSelect={(symbol) => void selectChartSymbol(symbol)}
            quotes={watchlistQuotes}
            signalCounts={signalCounts()}
          />
          <PositionsPanel initiallyOpen={false} mobileCollapsible={true} {portfolio} />
        </div>

        <button
          aria-label="Resize left panel"
          class="pane-resizer left-resizer"
          onpointerdown={(event) => resizeColumnPane(event, "left")}
          type="button"
        ></button>

        <div
          class="dashboard-column center"
          data-chart-mode={chartMode}
          style={centerColumnStyle()}
        >
          <section class="chart-toolbar" aria-label="Chart controls">
            <div class="chart-toolbar-main">
              <div class="symbol-combobox" onfocusout={onSymbolSelectorFocusOut}>
                <label for="chart-symbol-search">Symbol</label>
                <input
                  id="chart-symbol-search"
                  aria-activedescendant={symbolSearchOpen ? `symbol-option-${activeSuggestionIndex}` : undefined}
                  aria-autocomplete="list"
                  aria-controls="symbol-results"
                  aria-expanded={symbolSearchOpen}
                  aria-haspopup="listbox"
                  bind:value={symbolSearch}
                  onfocus={() => {
                    symbolSearchOpen = true;
                    syncSymbolSearch();
                  }}
                  oninput={() => {
                    symbolSearchOpen = true;
                    activeSuggestionIndex = 0;
                  }}
                  onkeydown={onSymbolSearchKeydown}
                  placeholder="BTC"
                  role="combobox"
                  type="search"
                />
                {#if symbolSearchOpen}
                  <div class="symbol-results" id="symbol-results" role="listbox">
                    {#if symbolSuggestions().length}
                      {#each symbolSuggestions() as suggestion, index (suggestion.symbol)}
                        <button
                          id={`symbol-option-${index}`}
                          class:active={index === activeSuggestionIndex}
                          aria-selected={index === activeSuggestionIndex}
                          onclick={() => void selectChartSymbol(suggestion.symbol)}
                          role="option"
                          type="button"
                        >
                          <strong>{suggestion.symbol}</strong>
                          <span>{suggestion.source}</span>
                        </button>
                      {/each}
                    {:else if normalizeMarketSymbol(symbolSearch)}
                      <button
                        class="active"
                        aria-selected="true"
                        onclick={() => void selectChartSymbol(symbolSearch)}
                        role="option"
                        type="button"
                      >
                        <strong>{normalizeMarketSymbol(symbolSearch)}</strong>
                        <span>custom</span>
                      </button>
                    {:else}
                      <p>No symbols</p>
                    {/if}
                  </div>
                {/if}
              </div>

              <div class="timeframe-group" aria-label="Chart timeframe">
                {#each ["1m", "5m", "15m", "1h", "4h", "1d", "1w"] as timeframe}
                  <button
                    class:active={timeframe === chartTimeframe}
                    disabled={isUpdatingChart}
                    onclick={() => void requestChartViewUpdate({ timeframe })}
                    type="button"
                  >
                    {timeframe}
                  </button>
                {/each}
              </div>

              <label class="source-select">
                <span>Source</span>
                <select
                  disabled={isUpdatingChart}
                  value={chartSource}
                  onchange={(event) => void requestChartViewUpdate({
                    source: event.currentTarget.value,
                  })}
                >
                  <option value="kai-api">kai-api</option>
                  <option value="coinbase">coinbase</option>
                </select>
              </label>

              <button
                disabled={isUpdatingChart}
                onclick={() => void resetChartSplit()}
                title="Reset the chart and chat split"
                type="button"
              >
                Reset Split
              </button>

              <button
                class:active={chartMode === "hide"}
                disabled={isUpdatingChart}
                onclick={() => void requestChartViewUpdate({ mode: "hide" })}
                title="Collapse the chart and give the center panel to chat"
                type="button"
              >
                Hide Chart
              </button>

              <button
                aria-label={chartSymbolIsWatched()
                  ? `Remove ${chartSymbol} from watchlist`
                  : `Add ${chartSymbol} to watchlist`}
                aria-pressed={chartSymbolIsWatched()}
                class:active={chartSymbolIsWatched()}
                class="toolbar-star"
                onclick={() => void toggleChartSymbolWatchlist()}
                title={chartSymbolIsWatched()
                  ? `Remove ${chartSymbol} from watchlist`
                  : `Add ${chartSymbol} to watchlist`}
                type="button"
              >
                {#if chartSymbolIsWatched()}★{:else}☆{/if}
              </button>

              <button
                class="toolbar-refresh"
                disabled={isUpdatingChart}
                onclick={() => void Promise.all([refreshSidebarData(), refreshChartData()])}
                type="button"
              >
                Refresh
              </button>
            </div>

            <div
              class:error={Boolean(chartUpdateError)}
              class:pending={isUpdatingChart}
              class="chart-toolbar-status"
            >
              <span>{chartUpdateLabel()}</span>
              <span>{formatPrice(chartPrice())}</span>
              <span
                class:negative={Boolean(chartQuote && typeof chartQuote.price_change_24h_pct === "number" && chartQuote.price_change_24h_pct < 0)}
                class:positive={Boolean(chartQuote && typeof chartQuote.price_change_24h_pct === "number" && chartQuote.price_change_24h_pct > 0)}
              >
                {formatChange(chartQuote?.price_change_24h_pct)}
              </span>
              <span>{streamLatencyLabel()}</span>
              <span>{streamThroughputLabel()}</span>
            </div>
          </section>

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
              <button
                onclick={() => void requestChartViewUpdate({ mode: lastVisibleChartMode })}
                type="button"
              >
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

          <button
            aria-label="Resize chart and chat panels"
            class="pane-resizer row-resizer"
            onpointerdown={resizeChartPane}
            type="button"
          ></button>

          <ChatPanel
            initiallyOpen={false}
            messages={chatMessages}
            mobileCollapsible={true}
            {streamingReply}
          />

          <form class="chat-input" onsubmit={onInputSubmit}>
            <div class="prompt-presets" aria-label="Chart analysis prompts">
              {#each chartAnalysisPrompts() as preset}
                <button
                  disabled={!daemonConnection}
                  onclick={() => void sendPromptPreset(preset.prompt)}
                  type="button"
                >
                  {preset.label}
                </button>
              {/each}
            </div>
            <textarea
              bind:value={inputDraft}
              onkeydown={onInputKeydown}
              placeholder="Type a prompt or slash command for this session"
              rows="3"
            ></textarea>
            <button type="submit">Send</button>
          </form>
        </div>

        <button
          aria-label="Resize right panel"
          class="pane-resizer right-resizer"
          onpointerdown={(event) => resizeColumnPane(event, "right")}
          type="button"
        ></button>

        <div class="dashboard-column right">
          <SignalPanel
            activeSymbol={chartSymbol}
            alerts={signalAlerts}
            initiallyOpen={false}
            mobileCollapsible={true}
            onSelect={(alert) => void selectSignalAlert(alert)}
            selectedId={selectedSignalId}
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
