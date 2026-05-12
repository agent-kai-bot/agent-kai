<script lang="ts">
  import { tick } from "svelte";
  import {
    formatAutoElapsed,
    formatToolElapsed,
    type AutoActivityState,
    type ChatActivityState,
    type ToolActivity,
  } from "$lib/chat-activity";
  import type { ChatHistoryEntry } from "$lib/daemon/types";
  import { renderMarkdown } from "$lib/markdown";
  import { formatChatTimestamp } from "$lib/market-ui";

  import Panel from "$lib/components/Panel.svelte";

  let {
    activity = null,
    messages,
    streamingReply = "",
    mobileCollapsible = false,
    initiallyOpen = true,
  }: {
    activity?: ChatActivityState | null;
    messages: ChatHistoryEntry[];
    streamingReply?: string;
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
  } = $props();

  const fallbackStreamingStartedAt = new Date().toISOString();

  let scrollAnchor: HTMLDivElement | undefined = $state();

  // Auto-scroll to bottom when messages change or streaming text grows.
  // Uses scrollIntoView on a sentinel element instead of scrollTop on
  // a specific container — this works regardless of which ancestor
  // (.chat-log or .panel-body) is the actual overflow-scroll target.
  $effect(() => {
    void messages.length;
    void streamingReply;
    void activity?.tools.length;
    void activity?.auto?.iterationsUsed;
    tick().then(() => {
      scrollAnchor?.scrollIntoView({ block: 'end', behavior: 'instant' });
    });
  });

  function label(role: string): string {
    if (role === "human") {
      return "User";
    }
    if (role === "system") {
      return "System";
    }
    return "Agent";
  }

  function hasActiveReply(): boolean {
    return Boolean(streamingReply || activity?.active);
  }

  function activeReplyTimestamp(): string | null {
    return formatChatTimestamp(activity?.startedAt ?? fallbackStreamingStartedAt);
  }

  function toolElapsed(tool: ToolActivity): string {
    return formatToolElapsed(tool.elapsedMs);
  }

  function autoBadgeLabel(auto: AutoActivityState): string {
    return `AUTO ${auto.iterationsUsed}/${auto.iterationsTotal} · ${formatAutoElapsed(auto.elapsedSeconds)}`;
  }

  function isTaskComplete(reason: string): boolean {
    return reason.trim().toLowerCase() === "task complete";
  }

  function shouldShowStatus(): boolean {
    return Boolean(
      activity?.active &&
        activity.statusActivity &&
        !activity.tools.some((tool) => tool.state === "running"),
    );
  }
</script>

<Panel
  eyebrow="Conversation"
  initiallyOpen={initiallyOpen}
  mobileCollapsible={mobileCollapsible}
  title="Chat"
  subtitle={`${messages.length} messages`}
>
  <div class="chat-log">
    {#if messages.length || hasActiveReply()}
      {#each messages as message, index (message.role + message.content + index)}
        <article class={`message ${message.role}`}>
          <header class="message-header">
            <span class="role">{label(message.role)}</span>
            {#if formatChatTimestamp(message.ts)}
              <span class="timestamp">{formatChatTimestamp(message.ts)}</span>
            {/if}
          </header>
          <div class="markdown-body">
            {@html renderMarkdown(message.content)}
          </div>
        </article>
      {/each}
      {#if hasActiveReply()}
        <article class="message ai streaming">
          <header class="message-header">
            <span class="role">Agent</span>
            {#if activeReplyTimestamp()}
              <span class="timestamp">{activeReplyTimestamp()}</span>
            {/if}
            {#if activity?.auto}
              <span
                class:complete={activity.auto.status === "stopped" && isTaskComplete(activity.auto.reason)}
                class:stopped={activity.auto.status === "stopped"}
                class="auto-badge"
                title={activity.auto.readonly ? "auto mode readonly" : "auto mode"}
              >
                {#if activity.auto.status === "stopped" && isTaskComplete(activity.auto.reason)}
                  <span class="auto-check">✓</span>
                {/if}
                <span>
                  {activity.auto.status === "running"
                    ? `[${autoBadgeLabel(activity.auto)}]`
                    : autoBadgeLabel(activity.auto)}
                </span>
                {#if activity.auto.status === "stopped" && activity.auto.reason && !isTaskComplete(activity.auto.reason)}
                  <span class="auto-reason">{activity.auto.reason}</span>
                {/if}
              </span>
            {/if}
            {#if shouldShowStatus()}
              <span class="activity-status">
                <span class="activity-spin">⟳</span>
                <span>{activity?.statusActivity}</span>
              </span>
            {/if}
          </header>
          {#if activity?.tools.length}
            <ul class="activity-tools" aria-label="Agent activity">
              {#each activity.tools as tool (tool.id)}
                <li
                  class:failed={tool.ok === false}
                  class:running={tool.state === "running"}
                >
                  {#if tool.state === "running"}
                    <span class="activity-icon activity-spin">⟳</span>
                    <span>
                      running tool:
                      <span class="tool-name">{tool.tool}</span>
                    </span>
                    {#if tool.argsPreview}
                      <code class="tool-args">args: {tool.argsPreview}</code>
                    {/if}
                  {:else}
                    <span class="activity-icon">{tool.ok === false ? "✗" : "✓"}</span>
                    <span class="tool-name">{tool.tool}</span>
                    {#if toolElapsed(tool)}
                      <span class="tool-elapsed">· {toolElapsed(tool)}</span>
                    {/if}
                    {#if tool.ok === false}
                      <span class="tool-failed">(failed)</span>
                    {/if}
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
          {#if streamingReply}
            <pre>{streamingReply}</pre>
          {/if}
        </article>
      {/if}
    {:else}
      <p class="empty">No chat history in this session yet.</p>
    {/if}
    <div bind:this={scrollAnchor} class="scroll-anchor"></div>
  </div>
</Panel>

<style>
  .chat-log {
    display: grid;
    gap: 0.75rem;
    min-height: 0;
    overflow-y: auto;
    padding-right: 0.2rem;
  }

  .message {
    display: grid;
    gap: 0.45rem;
    border-radius: 1rem;
    padding: 0.9rem;
  }

  .message.human {
    background: rgba(77, 211, 168, 0.12);
    border: 1px solid rgba(77, 211, 168, 0.18);
  }

  .message.ai,
  .message.system {
    background: rgba(7, 19, 31, 0.68);
    border: 1px solid rgba(145, 181, 221, 0.08);
  }

  .message.streaming {
    border-style: dashed;
  }

  .message-header {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.6rem;
  }

  .message-header .role {
    color: var(--accent-strong);
    font-size: 0.76rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .message-header .timestamp {
    color: var(--muted);
    font-size: 0.7rem;
    font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
    letter-spacing: 0.02em;
  }

  .auto-badge,
  .activity-status {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    min-width: 0;
    font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
    font-size: 0.68rem;
    letter-spacing: 0.02em;
    line-height: 1.35;
  }

  .auto-badge {
    border: 1px solid rgba(77, 211, 168, 0.22);
    border-radius: 0.45rem;
    background: rgba(77, 211, 168, 0.08);
    color: var(--accent-strong);
    padding: 0.12rem 0.42rem;
  }

  .auto-badge.stopped {
    border-color: rgba(145, 181, 221, 0.16);
    background: rgba(145, 181, 221, 0.06);
    color: var(--muted);
  }

  .auto-badge.complete {
    border-color: rgba(77, 211, 168, 0.28);
    color: var(--accent-strong);
  }

  .auto-check {
    color: var(--accent-strong);
    font-weight: 700;
  }

  .auto-reason {
    color: var(--muted);
    overflow-wrap: anywhere;
  }

  .activity-status {
    color: var(--accent-strong);
  }

  .activity-tools {
    display: grid;
    gap: 0.3rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .activity-tools li {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.35rem;
    min-width: 0;
    color: var(--muted);
    font-size: 0.78rem;
    line-height: 1.45;
  }

  .activity-tools li.running {
    color: var(--text);
  }

  .activity-tools li.failed {
    color: #ff8a8a;
  }

  .activity-icon {
    width: 1rem;
    color: var(--accent-strong);
    font-weight: 700;
    text-align: center;
  }

  .activity-tools li.failed .activity-icon,
  .tool-failed {
    color: #ff8a8a;
  }

  .tool-name,
  .tool-args,
  .tool-elapsed {
    font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  }

  .tool-name {
    color: var(--text);
    overflow-wrap: anywhere;
  }

  .tool-args {
    max-width: 100%;
    border: 1px solid rgba(145, 181, 221, 0.1);
    border-radius: 0.35rem;
    background: rgba(2, 8, 14, 0.36);
    color: var(--muted);
    padding: 0.08rem 0.3rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .tool-elapsed {
    color: var(--muted);
  }

  .activity-spin {
    display: inline-block;
    animation: activity-spin 1s linear infinite;
  }

  .message pre {
    margin: 0;
    color: var(--text);
    font: inherit;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
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
    line-height: 1.65;
  }

  .message :global(h1),
  .message :global(h2),
  .message :global(h3),
  .message :global(h4) {
    margin: 1rem 0 0.5rem;
    line-height: 1.2;
  }

  .message :global(ul),
  .message :global(ol) {
    margin: 0.6rem 0;
    padding-left: 1.35rem;
  }

  .message :global(pre) {
    overflow: auto;
    border-radius: 0.8rem;
    background: rgba(2, 8, 14, 0.72);
    padding: 0.8rem;
  }

  .message :global(code) {
    font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  }

  .message :global(table) {
    width: 100%;
    border-collapse: collapse;
    margin: 0.75rem 0;
  }

  .message :global(th),
  .message :global(td) {
    border: 1px solid rgba(145, 181, 221, 0.1);
    padding: 0.45rem 0.55rem;
    text-align: left;
  }

  .message :global(a) {
    color: var(--accent);
  }

  .scroll-anchor {
    height: 1px;
    width: 100%;
  }

  .empty {
    margin: 0;
    color: var(--muted);
  }

  @keyframes activity-spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }

  @media (max-width: 700px) {
    .chat-log {
      max-height: 36vh;
      overflow: auto;
    }
  }
</style>
