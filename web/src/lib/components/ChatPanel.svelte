<script lang="ts">
  import { tick } from "svelte";
  import type { ChatHistoryEntry } from "$lib/daemon/types";
  import { renderMarkdown } from "$lib/markdown";
  import { formatChatTimestamp } from "$lib/market-ui";

  import Panel from "$lib/components/Panel.svelte";

  let {
    messages,
    streamingReply = "",
    mobileCollapsible = false,
    initiallyOpen = true,
  }: {
    messages: ChatHistoryEntry[];
    streamingReply?: string;
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
  } = $props();

  const streamingStartedAt = new Date().toISOString();

  let scrollAnchor: HTMLDivElement | undefined = $state();

  // Auto-scroll to bottom when messages change or streaming text grows.
  // Uses scrollIntoView on a sentinel element instead of scrollTop on
  // a specific container — this works regardless of which ancestor
  // (.chat-log or .panel-body) is the actual overflow-scroll target.
  $effect(() => {
    void messages.length;
    void streamingReply;
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
</script>

<Panel
  eyebrow="Conversation"
  initiallyOpen={initiallyOpen}
  mobileCollapsible={mobileCollapsible}
  title="Chat"
  subtitle={`${messages.length} messages`}
>
  <div class="chat-log">
    {#if messages.length || streamingReply}
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
      {#if streamingReply}
        <article class="message ai streaming">
          <header class="message-header">
            <span class="role">Agent</span>
            <span class="timestamp">{formatChatTimestamp(streamingStartedAt)}</span>
          </header>
          <pre>{streamingReply}</pre>
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

  @media (max-width: 700px) {
    .chat-log {
      max-height: 36vh;
      overflow: auto;
    }
  }
</style>
