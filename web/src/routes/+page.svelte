<script lang="ts">
  import { onDestroy, onMount } from "svelte";

  import {
    DaemonClient,
    DEFAULT_SESSION_NAME,
  } from "$lib/daemon/client";
  import { readStoredToken, writeStoredToken } from "$lib/daemon/storage";
  import type {
    ErrorEnvelope,
    ServerEnvelope,
    SessionSummary,
  } from "$lib/daemon/types";
  import { landingCards } from "$lib/web-shell";

  const client = new DaemonClient();
  const localhostHosts = new Set(["localhost", "127.0.0.1", "::1"]);

  let token = "";
  let sessionName = DEFAULT_SESSION_NAME;
  let knownSessions: SessionSummary[] = [];
  let connectionStatus = "checking daemon...";
  let attachError = "";
  let isConnecting = false;
  let tokenRequired = false;
  let activeSession = "";
  let currentStatus = "idle";
  let queueDepth = 0;
  let watchlist: string[] = [];
  let snapshotSummary = "";
  let recentEvents: string[] = [];
  let daemonConnection: Awaited<ReturnType<DaemonClient["attach"]>> | null = null;

  function logEvent(summary: string): void {
    recentEvents = [summary, ...recentEvents].slice(0, 8);
  }

  function summarizeEnvelope(envelope: ServerEnvelope): string {
    if (envelope.type === "status") {
      return `status: ${envelope.activity} (queue ${envelope.queue})`;
    }
    if (envelope.type === "token") {
      return `token: ${envelope.text}`;
    }
    if (envelope.type === "final") {
      return `final: ${envelope.text.slice(0, 80)}`;
    }
    if (envelope.type === "error") {
      return `error: ${envelope.message}`;
    }
    if (envelope.type === "tool_start") {
      return `tool start: ${envelope.tool}`;
    }
    if (envelope.type === "tool_end") {
      return `tool end: ${envelope.tool}`;
    }
    return envelope.type.replaceAll("_", " ");
  }

  function applyEnvelope(envelope: ServerEnvelope): void {
    if (envelope.type === "status") {
      currentStatus = envelope.activity;
      queueDepth = envelope.queue;
    } else if (envelope.type === "error") {
      attachError = envelope.message;
    }
    logEvent(summarizeEnvelope(envelope));
  }

  function applySnapshot(): void {
    if (!daemonConnection) {
      snapshotSummary = "";
      watchlist = [];
      return;
    }
    const snapshot = daemonConnection.snapshot;
    watchlist = snapshot.watchlist_symbols;
    snapshotSummary = `${snapshot.chart_symbol} ${snapshot.chart_timeframe} · ${snapshot.chart_source} · ${snapshot.chat_history.length} chat messages`;
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
      daemonConnection?.close(1000, "reconnect");
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
      };
      activeSession = daemonConnection.session;
      currentStatus = daemonConnection.activityStatus;
      queueDepth = daemonConnection.queueDepth;
      connectionStatus = `attached to session ${activeSession}`;
      applySnapshot();
      logEvent(`session attached: ${activeSession}`);
      await refreshSessions();
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
    watchlist = [];
  }

  function onConnectSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void attachSession();
  }

  onMount(() => {
    token = readStoredToken();
    tokenRequired = !localhostHosts.has(window.location.hostname);
    void refreshSessions();
  });

  onDestroy(() => {
    daemonConnection?.close(1000, "page teardown");
  });
</script>

<svelte:head>
  <title>KAI Web Terminal</title>
  <meta
    name="description"
    content="Browser client for the daemon-backed KAI trading terminal."
  />
</svelte:head>

<section class="landing-shell">
  <div class="landing-card">
    <p class="eyebrow">Daemon Client</p>
    <h1>KAI Web Terminal</h1>
    <p class="summary">
      The browser client attaches to the same daemon sessions as the terminal and
      reuses the daemon websocket protocol directly. This slice adds the browser
      attach flow and local token storage so the later dashboard can build on a
      real session transport instead of a mock shell.
    </p>

    <div class="status-banner">
      <span>{connectionStatus}</span>
      {#if activeSession}
        <strong>{activeSession}</strong>
      {/if}
    </div>

    <div class="connect-grid">
      <form class="connect-panel" on:submit={onConnectSubmit}>
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

        <datalist id="known-sessions">
          {#each knownSessions as session (session.name)}
            <option value={session.name}></option>
          {/each}
        </datalist>

        <div class="button-row">
          <button disabled={isConnecting} type="submit">
            {#if isConnecting}Attaching...{:else}Attach Session{/if}
          </button>
          <button disabled={!daemonConnection} on:click={disconnectSession} type="button">
            Disconnect
          </button>
          <button disabled={isConnecting} on:click={() => void refreshSessions()} type="button">
            Refresh
          </button>
        </div>

        <p class="token-hint">
          Remote clients should use the daemon bearer token from
          <code>workspaces/daemon-token.txt</code>. Localhost attaches can continue
          without a token until Phase 7 enables enforcement.
        </p>

        {#if attachError}
          <p class="error-text">{attachError}</p>
        {/if}
      </form>

      <div class="session-panel">
        <h2>Session Snapshot</h2>
        {#if daemonConnection}
          <p>{snapshotSummary}</p>
          <p>status: {currentStatus} · queue: {queueDepth}</p>
          <div class="chip-row">
            {#each watchlist as symbol (symbol)}
              <span>{symbol}</span>
            {/each}
          </div>
        {:else}
          <p>No session attached yet.</p>
        {/if}

        <h3>Known Sessions</h3>
        <ul class="session-list">
          {#each knownSessions as session (session.name)}
            <li>
              <strong>{session.name}</strong>
              <span>{session.activity_status ?? "idle"}</span>
            </li>
          {/each}
        </ul>
      </div>
    </div>

    <div class="event-panel">
      <div class="panel-header">
        <h2>Protocol Activity</h2>
        <span>{recentEvents.length} recent</span>
      </div>
      <ul class="event-list">
        {#each recentEvents as eventLine, index (eventLine + index)}
          <li>{eventLine}</li>
        {/each}
      </ul>
    </div>

    <dl class="checklist">
      {#each landingCards as card (card.title)}
        <div>
          <dt>{card.title}</dt>
          <dd>{card.detail}</dd>
        </div>
      {/each}
    </dl>
  </div>
</section>
