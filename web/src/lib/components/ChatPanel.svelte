<script lang="ts">
  import type { ChatHistoryEntry } from "$lib/daemon/types";
  import { renderMarkdown } from "$lib/markdown";

  import Panel from "$lib/components/Panel.svelte";

  let {
    messages,
    streamingReply = "",
  }: {
    messages: ChatHistoryEntry[];
    streamingReply?: string;
  } = $props();

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

<Panel eyebrow="Conversation" title="Chat" subtitle={`${messages.length} messages`}>
  <div class="chat-log">
    {#if messages.length || streamingReply}
      {#each messages as message, index (message.role + message.content + index)}
        <article class={`message ${message.role}`}>
          <span>{label(message.role)}</span>
          <div class="markdown-body">
            {@html renderMarkdown(message.content)}
          </div>
        </article>
      {/each}
      {#if streamingReply}
        <article class="message ai streaming">
          <span>Agent</span>
          <pre>{streamingReply}</pre>
        </article>
      {/if}
    {:else}
      <p class="empty">No chat history in this session yet.</p>
    {/if}
  </div>
</Panel>

<style>
  .chat-log {
    display: grid;
    gap: 0.75rem;
    max-height: 32rem;
    overflow: auto;
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

  .message span {
    color: var(--accent-strong);
    font-size: 0.76rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
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

  .empty {
    margin: 0;
    color: var(--muted);
  }
</style>
