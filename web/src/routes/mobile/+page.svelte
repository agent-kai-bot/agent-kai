<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";
  import {
    DaemonClient,
    DEFAULT_HTTP_BASE_URL,
    DEFAULT_SESSION_NAME,
    deriveHttpBaseUrl,
    type DaemonConnection,
  } from "$lib/daemon/client";
  import {
    DAEMON_TOKEN_STORAGE_KEY,
    readStoredToken,
    writeStoredToken,
  } from "$lib/daemon/storage";
  import { formatDaemonError } from "$lib/daemon/error";
  import type { ChatHistoryEntry, ServerEnvelope, SessionSummary } from "$lib/daemon/types";
  import {
    applyMobileEnvelope,
    emptyMobileChatState,
    mobileChatStateFromSnapshot,
    MOBILE_BASE_URL_STORAGE_KEY,
    MOBILE_SESSION_STORAGE_KEY,
    normalizeDaemonBaseUrl,
    parseOutgoingMobileMessage,
    startMobileUserTurn,
    type MobileChatState,
  } from "$lib/mobile-chat";
  import { formatChatTimestamp } from "$lib/market-ui";
  import { renderMarkdown } from "$lib/markdown";

  let baseUrl = $state(DEFAULT_HTTP_BASE_URL);
  let token = $state("");
  let sessionName = $state(DEFAULT_SESSION_NAME);
  let client = $state(new DaemonClient({ baseHttpUrl: DEFAULT_HTTP_BASE_URL }));
  let connection = $state<DaemonConnection | null>(null);
  let chat = $state<MobileChatState>(emptyMobileChatState());
  let inputDraft = $state("");
  let knownSessions = $state<SessionSummary[]>([]);
  let isConnecting = $state(false);
  let isRefreshingSessions = $state(false);
  let settingsOpen = $state(true);
  // Set once a session is attached, cleared on a deliberate disconnect. Gates
  // the resume-reconnect so we only re-attach sessions the user was actually in.
  let autoReconnect = $state(false);
  let connectionStatus = $state("disconnected");
  let attachError = $state("");
  let sessionListError = $state("");
  let scrollAnchor: HTMLDivElement | undefined = $state();

  const connected = $derived(Boolean(connection));
  const canSend = $derived(Boolean(connection && inputDraft.trim()));

  function readStorageValue(key: string, fallback = ""): string {
    if (typeof window === "undefined") {
      return fallback;
    }
    try {
      return window.localStorage.getItem(key)?.trim() || fallback;
    } catch {
      return fallback;
    }
  }

  function writeStorageValue(key: string, value: string): void {
    if (typeof window === "undefined") {
      return;
    }
    try {
      const normalized = value.trim();
      if (normalized) {
        window.localStorage.setItem(key, normalized);
      } else {
        window.localStorage.removeItem(key);
      }
    } catch {
      // Storage is optional for mobile private browsing modes.
    }
  }

  function currentClient(): DaemonClient {
    const normalized = normalizeDaemonBaseUrl(baseUrl, deriveHttpBaseUrl());
    baseUrl = normalized;
    const next = new DaemonClient({ baseHttpUrl: normalized });
    client = next;
    return next;
  }

  async function refreshSessions(): Promise<void> {
    isRefreshingSessions = true;
    sessionListError = "";
    try {
      knownSessions = await currentClient().listSessions(token);
      if (!sessionName.trim() && knownSessions[0]) {
        sessionName = knownSessions[0].name;
      }
    } catch (error) {
      knownSessions = [];
      sessionListError = error instanceof Error ? error.message : String(error);
    } finally {
      isRefreshingSessions = false;
    }
  }

  function handleEnvelope(envelope: ServerEnvelope): void {
    chat = applyMobileEnvelope(chat, envelope);
    if (envelope.type === "session_attached") {
      sessionName = envelope.session;
      connectionStatus = `attached to ${envelope.session}`;
    } else if (envelope.type === "status") {
      connectionStatus = `${envelope.activity} · q${envelope.queue}`;
    } else if (envelope.type === "error") {
      attachError = formatDaemonError(envelope);
    }
  }

  function handleClose(code?: number): void {
    connection = null;
    connectionStatus = `disconnected (${code ?? 1000})`;
    chat = {
      ...chat,
      status: "disconnected",
      queueDepth: 0,
    };
  }

  async function attachSession(): Promise<void> {
    attachError = "";
    sessionListError = "";
    isConnecting = true;
    writeStorageValue(MOBILE_BASE_URL_STORAGE_KEY, baseUrl);
    writeStorageValue(MOBILE_SESSION_STORAGE_KEY, sessionName);
    writeStoredToken(token, DAEMON_TOKEN_STORAGE_KEY);

    try {
      const nextClient = currentClient();
      if (connection) {
        connection.onClose = undefined;
        connection.close(1000, "mobile reconnect");
      }

      const nextConnection = await nextClient.attach({
        session: sessionName,
        token,
        createIfMissing: true,
      });
      nextConnection.onEnvelope = handleEnvelope;
      nextConnection.onClose = handleClose;
      connection = nextConnection;
      sessionName = nextConnection.session;
      chat = mobileChatStateFromSnapshot(nextConnection.snapshot, {
        session: nextConnection.session,
        status: nextConnection.activityStatus,
        queueDepth: nextConnection.queueDepth,
      });
      connectionStatus = `${nextConnection.activityStatus} · q${nextConnection.queueDepth}`;
      settingsOpen = false;
      autoReconnect = true;
      void refreshSessions();
    } catch (error) {
      attachError = error instanceof Error ? error.message : String(error);
      connectionStatus = "attach failed";
      connection = null;
    } finally {
      isConnecting = false;
    }
  }

  function disconnectSession(): void {
    autoReconnect = false;
    connection?.close(1000, "mobile disconnect");
    connection = null;
    connectionStatus = "disconnected";
    chat = {
      ...chat,
      status: "disconnected",
      queueDepth: 0,
    };
  }

  function stopCurrentRun(): void {
    connection?.interrupt();
  }

  async function sendMessage(): Promise<void> {
    const activeConnection = connection;
    const outgoing = parseOutgoingMobileMessage(inputDraft);
    if (!activeConnection || !outgoing) {
      return;
    }

    inputDraft = "";
    chat = startMobileUserTurn(chat, outgoing.text);
    if (outgoing.kind === "slash") {
      activeConnection.sendSlash(outgoing.command, outgoing.args);
    } else {
      activeConnection.sendInput(outgoing.text);
    }
  }

  function onConnectSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void attachSession();
  }

  function onMessageSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void sendMessage();
  }

  function onComposerKeydown(event: KeyboardEvent): void {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    event.preventDefault();
    void sendMessage();
  }

  function roleLabel(message: ChatHistoryEntry): string {
    if (message.role === "human") {
      return "You";
    }
    if (message.role === "system") {
      return "System";
    }
    return "KAI";
  }

  onMount(() => {
    baseUrl = readStorageValue(MOBILE_BASE_URL_STORAGE_KEY, deriveHttpBaseUrl());
    sessionName = readStorageValue(MOBILE_SESSION_STORAGE_KEY, DEFAULT_SESSION_NAME);
    const urlToken =
      new URLSearchParams(window.location.search).get("token")?.trim() ?? "";
    if (urlToken) {
      writeStoredToken(urlToken, DAEMON_TOKEN_STORAGE_KEY);
      window.history.replaceState(
        {},
        document.title,
        window.location.pathname + window.location.hash,
      );
    }
    token = urlToken || readStoredToken(DAEMON_TOKEN_STORAGE_KEY);
    client = new DaemonClient({ baseHttpUrl: normalizeDaemonBaseUrl(baseUrl, deriveHttpBaseUrl()) });
    void refreshSessions();

    // Phones suspend the tab on lock and drop the websocket, which re-shows the
    // attach sheet on resume. Silently re-attach the prior session instead.
    const onVisibility = () => {
      if (
        document.visibilityState === "visible" &&
        autoReconnect &&
        !connection &&
        !isConnecting &&
        token.trim() &&
        sessionName.trim()
      ) {
        void attachSession();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
    };
  });

  onDestroy(() => {
    connection?.close(1000, "mobile page teardown");
  });

  $effect(() => {
    void chat.messages.length;
    void chat.streamingReply;
    void chat.activity.tools.length;
    void chat.activity.auto?.iterationsUsed;
    tick().then(() => {
      scrollAnchor?.scrollIntoView({ block: "end", behavior: "smooth" });
    });
  });

  $effect(() => {
    if (typeof document === "undefined") {
      return;
    }
    document.body.classList.add("mobile-active");
    return () => {
      document.body.classList.remove("mobile-active");
    };
  });
</script>

<svelte:head>
  <title>KAI Mobile</title>
  <meta
    name="description"
    content="Mobile chat client for daemon-backed Agent KAI sessions."
  />
</svelte:head>

<main class="mobile-shell">
  <header class="mobile-topbar">
    <div class="session-title">
      <span class:online={connected} class="status-dot"></span>
      <div>
        <p>KAI Mobile</p>
        <strong>{connected ? sessionName : "No session"}</strong>
      </div>
    </div>

    <div class="topbar-actions">
      {#if connected}
        <button
          class="plain-button"
          disabled={chat.status === "idle"}
          onclick={stopCurrentRun}
          type="button"
        >
          Stop
        </button>
      {/if}
      <button
        aria-expanded={settingsOpen}
        class="plain-button"
        onclick={() => {
          settingsOpen = !settingsOpen;
        }}
        type="button"
      >
        Server
      </button>
    </div>
  </header>

  {#if settingsOpen || !connected}
    <section class="connection-sheet" aria-label="Connection">
      <form class="connection-form" onsubmit={onConnectSubmit}>
        <label>
          <span>Daemon URL</span>
          <input
            bind:value={baseUrl}
            inputmode="url"
            name="base-url"
            placeholder="https://kai.example.com"
            required
            type="url"
          />
        </label>

        <label>
          <span>Session</span>
          <input
            bind:value={sessionName}
            list="mobile-known-sessions"
            name="session"
            placeholder="terminal"
            required
            type="text"
          />
          <datalist id="mobile-known-sessions">
            {#each knownSessions as session (session.name)}
              <option value={session.name}>{session.activity_status ?? "idle"}</option>
            {/each}
          </datalist>
        </label>

        <label>
          <span>Token</span>
          <input
            bind:value={token}
            autocomplete="current-password"
            name="token"
            placeholder="Bearer token"
            type="password"
          />
        </label>

        <div class="connection-actions">
          <button disabled={isConnecting} type="submit">
            {#if isConnecting}Connecting...{:else if connected}Reconnect{:else}Connect{/if}
          </button>
          <button
            disabled={isRefreshingSessions}
            onclick={() => void refreshSessions()}
            type="button"
          >
            {#if isRefreshingSessions}Refreshing...{:else}Refresh{/if}
          </button>
          {#if connected}
            <button class="danger" onclick={disconnectSession} type="button">
              Disconnect
            </button>
          {/if}
        </div>
      </form>

      {#if knownSessions.length}
        <div class="session-strip" aria-label="Known sessions">
          {#each knownSessions as session (session.name)}
            <button
              class:active={session.name === sessionName}
              onclick={() => {
                sessionName = session.name;
              }}
              type="button"
            >
              <strong>{session.name}</strong>
              <span>{session.activity_status ?? "idle"}</span>
            </button>
          {/each}
        </div>
      {/if}

      {#if attachError || sessionListError}
        <p class="error-text">{attachError || sessionListError}</p>
      {/if}
    </section>
  {/if}

  <section class="chat-status" aria-live="polite">
    <span>{connectionStatus}</span>
    <span>{chat.historyLabel}</span>
  </section>

  <section class="chat-surface" aria-label="Session chat">
    {#if chat.messages.length || chat.streamingReply || chat.activity.active}
      {#each chat.messages as message, index (message.role + message.content + index)}
        <article class:self={message.role === "human"} class="message">
          <header>
            <span>{roleLabel(message)}</span>
            {#if formatChatTimestamp(message.ts)}
              <time datetime={message.ts}>{formatChatTimestamp(message.ts)}</time>
            {/if}
          </header>
          <div class="markdown-body">
            {@html renderMarkdown(message.content)}
          </div>
        </article>
      {/each}

      {#if chat.activity.tools.length}
        <div class="activity-list" aria-label="Agent activity">
          {#each chat.activity.tools as tool (tool.id)}
            <p class:failed={tool.ok === false}>
              <span>{tool.state === "running" ? "running" : tool.ok === false ? "failed" : "done"}</span>
              <strong>{tool.tool}</strong>
            </p>
          {/each}
        </div>
      {/if}

      {#if chat.streamingReply || chat.activity.active}
        <article class="message streaming">
          <header>
            <span>KAI</span>
            {#if chat.activity.statusActivity}
              <small>{chat.activity.statusActivity}</small>
            {/if}
          </header>
          {#if chat.streamingReply}
            <pre>{chat.streamingReply}</pre>
          {/if}
        </article>
      {/if}
    {:else}
      <p class="empty-state">No chat history.</p>
    {/if}

    <div bind:this={scrollAnchor} class="scroll-anchor"></div>
  </section>

  <form class="composer" onsubmit={onMessageSubmit}>
    <textarea
      bind:value={inputDraft}
      disabled={!connected}
      onkeydown={onComposerKeydown}
      placeholder={connected ? "Message KAI" : "Connect to a session"}
      rows="1"
    ></textarea>
    <button aria-label="Send message" disabled={!canSend} type="submit">
      Send
    </button>
  </form>
</main>

<style>
  :global(body.mobile-active) {
    overflow: hidden;
  }

  .mobile-shell {
    display: flex;
    flex-direction: column;
    width: min(100%, 48rem);
    height: 100dvh;
    min-height: 100dvh;
    margin: 0 auto;
    background:
      linear-gradient(180deg, rgba(7, 18, 31, 0.98), rgba(9, 22, 34, 0.98)),
      var(--bg);
    color: var(--text);
    overflow: hidden;
  }

  /* Fixed-height bands never shrink; the chat surface absorbs all remaining
     space and scrolls internally. This keeps the composer pinned to the
     bottom whether or not the connection sheet is mounted (the {#if} block
     changes the child count, which a fixed grid-template-rows mis-tracked,
     collapsing the composer to 0px when connected). */
  .mobile-topbar,
  .connection-sheet,
  .chat-status,
  .composer {
    flex: none;
  }

  .mobile-topbar,
  .chat-status,
  .composer {
    padding-left: max(1rem, env(safe-area-inset-left));
    padding-right: max(1rem, env(safe-area-inset-right));
  }

  .mobile-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding-top: max(0.85rem, env(safe-area-inset-top));
    padding-bottom: 0.75rem;
    border-bottom: 1px solid rgba(145, 181, 221, 0.14);
    background: rgba(5, 14, 24, 0.92);
  }

  .session-title {
    display: flex;
    align-items: center;
    min-width: 0;
    gap: 0.75rem;
  }

  .session-title div {
    min-width: 0;
  }

  .session-title p,
  .session-title strong {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-title p {
    margin: 0 0 0.1rem;
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .session-title strong {
    font-size: 1.05rem;
  }

  .status-dot {
    width: 0.68rem;
    height: 0.68rem;
    flex: 0 0 auto;
    border-radius: 999px;
    background: rgba(145, 181, 221, 0.46);
    box-shadow: 0 0 0 0.28rem rgba(145, 181, 221, 0.08);
  }

  .status-dot.online {
    background: var(--accent);
    box-shadow: 0 0 0 0.28rem rgba(77, 211, 168, 0.12);
  }

  .topbar-actions,
  .connection-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  button {
    min-height: 2.5rem;
    border: 0;
    border-radius: 0.65rem;
    background: linear-gradient(135deg, #4dd3a8, #72d7f3);
    color: #052334;
    cursor: pointer;
    font: inherit;
    font-weight: 800;
    padding: 0.65rem 0.85rem;
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.48;
  }

  .plain-button,
  .connection-actions button:nth-child(2) {
    border: 1px solid rgba(145, 181, 221, 0.16);
    background: rgba(145, 181, 221, 0.1);
    color: var(--text);
  }

  .danger {
    border: 1px solid rgba(255, 138, 138, 0.26);
    background: rgba(255, 138, 138, 0.1);
    color: #ffb5b5;
  }

  .connection-sheet {
    display: grid;
    gap: 0.85rem;
    border-bottom: 1px solid rgba(145, 181, 221, 0.14);
    background: rgba(8, 20, 32, 0.96);
    padding: 0.9rem max(1rem, env(safe-area-inset-right)) 1rem
      max(1rem, env(safe-area-inset-left));
  }

  .connection-form {
    display: grid;
    gap: 0.75rem;
  }

  label {
    display: grid;
    gap: 0.35rem;
  }

  label span {
    color: var(--accent-strong);
    font-size: 0.72rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  input,
  textarea {
    width: 100%;
    border: 1px solid rgba(145, 181, 221, 0.18);
    border-radius: 0.7rem;
    background: rgba(2, 8, 14, 0.72);
    color: var(--text);
    font: inherit;
  }

  input {
    min-height: 2.7rem;
    padding: 0.7rem 0.8rem;
  }

  input:focus,
  textarea:focus,
  button:focus-visible {
    outline: 2px solid rgba(77, 211, 168, 0.42);
    outline-offset: 2px;
  }

  .session-strip {
    display: flex;
    gap: 0.55rem;
    overflow-x: auto;
    padding-bottom: 0.15rem;
  }

  .session-strip button {
    display: grid;
    min-width: 8.5rem;
    gap: 0.15rem;
    border: 1px solid rgba(145, 181, 221, 0.16);
    background: rgba(145, 181, 221, 0.08);
    color: var(--text);
    text-align: left;
  }

  .session-strip button.active {
    border-color: rgba(77, 211, 168, 0.42);
  }

  .session-strip strong,
  .session-strip span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-strip span {
    color: var(--muted);
    font-size: 0.78rem;
  }

  .error-text {
    margin: 0;
    color: #ffb5b5;
    font-size: 0.88rem;
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .chat-status {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid rgba(145, 181, 221, 0.1);
    background: rgba(5, 14, 24, 0.64);
    color: var(--muted);
    font-size: 0.78rem;
    padding-top: 0.55rem;
    padding-bottom: 0.55rem;
  }

  .chat-status span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chat-surface {
    display: grid;
    align-content: start;
    gap: 0.75rem;
    flex: 1 1 0;
    min-height: 0;
    overflow-y: auto;
    padding: 1rem max(1rem, env(safe-area-inset-right)) 1rem
      max(1rem, env(safe-area-inset-left));
  }

  .message {
    display: grid;
    max-width: min(100%, 38rem);
    gap: 0.42rem;
    justify-self: start;
    border: 1px solid rgba(145, 181, 221, 0.1);
    border-radius: 0.8rem;
    background: rgba(7, 19, 31, 0.76);
    padding: 0.8rem 0.85rem;
    overflow-wrap: anywhere;
  }

  .message.self {
    justify-self: end;
    border-color: rgba(77, 211, 168, 0.2);
    background: rgba(77, 211, 168, 0.12);
  }

  .message.streaming {
    border-style: dashed;
  }

  .message header {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.45rem;
  }

  .message header span {
    color: var(--accent-strong);
    font-size: 0.72rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .message time,
  .message small {
    color: var(--muted);
    font-size: 0.72rem;
  }

  .message pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
    font: inherit;
    line-height: 1.55;
  }

  .message :global(.markdown-body > :first-child) {
    margin-top: 0;
  }

  .message :global(.markdown-body > :last-child) {
    margin-bottom: 0;
  }

  .message :global(p),
  .message :global(li),
  .message :global(blockquote) {
    color: var(--text);
    line-height: 1.58;
  }

  .message :global(ul),
  .message :global(ol) {
    margin: 0.55rem 0;
    padding-left: 1.25rem;
  }

  .message :global(pre) {
    overflow: auto;
    border-radius: 0.65rem;
    background: rgba(2, 8, 14, 0.74);
    padding: 0.75rem;
  }

  .message :global(code) {
    font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  }

  .message :global(a) {
    color: var(--accent);
  }

  .activity-list {
    display: grid;
    gap: 0.35rem;
    justify-self: start;
    max-width: 100%;
  }

  .activity-list p {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin: 0;
    border: 1px solid rgba(145, 181, 221, 0.1);
    border-radius: 0.65rem;
    background: rgba(145, 181, 221, 0.06);
    color: var(--muted);
    font-size: 0.8rem;
    padding: 0.42rem 0.58rem;
  }

  .activity-list p.failed {
    color: #ffb5b5;
  }

  .activity-list strong {
    color: var(--text);
    overflow-wrap: anywhere;
  }

  .empty-state {
    align-self: center;
    justify-self: center;
    margin: 20vh 0 0;
    color: var(--muted);
  }

  .scroll-anchor {
    height: 1px;
  }

  .composer {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: end;
    gap: 0.65rem;
    border-top: 1px solid rgba(145, 181, 221, 0.14);
    background: rgba(5, 14, 24, 0.96);
    padding-top: 0.75rem;
    padding-bottom: max(0.75rem, env(safe-area-inset-bottom));
  }

  .composer textarea {
    min-height: 2.8rem;
    max-height: 9rem;
    resize: vertical;
    padding: 0.78rem 0.85rem;
    line-height: 1.45;
  }

  .composer button {
    min-width: 4.75rem;
    min-height: 2.8rem;
  }

  @media (min-width: 48rem) {
    .mobile-shell {
      border-left: 1px solid rgba(145, 181, 221, 0.12);
      border-right: 1px solid rgba(145, 181, 221, 0.12);
    }
  }
</style>
