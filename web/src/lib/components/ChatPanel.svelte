<script lang="ts">
  import type { ChatHistoryEntry } from "$lib/daemon/types";

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
          <pre>{message.content}</pre>
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

  .empty {
    margin: 0;
    color: var(--muted);
  }
</style>
