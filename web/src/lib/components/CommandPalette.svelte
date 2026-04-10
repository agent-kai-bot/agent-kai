<script lang="ts">
  import { tick } from "svelte";

  import type { CommandPaletteItem } from "$lib/command-palette";

  let inputElement = $state<HTMLInputElement | null>(null);

  let {
    open,
    query,
    items,
    activeSession = "",
    onClose,
    onSelect,
    onQueryChange,
  }: {
    open: boolean;
    query: string;
    items: CommandPaletteItem[];
    activeSession?: string;
    onClose: () => void;
    onSelect: (command: string) => void;
    onQueryChange: (value: string) => void;
  } = $props();

  $effect(() => {
    if (!open) {
      return;
    }
    void tick().then(() => inputElement?.focus());
  });
</script>

{#if open}
  <div class="palette-backdrop" onclick={onClose} role="presentation"></div>
  <div
    aria-label="Slash command palette"
    aria-modal="true"
    class="palette-dialog"
    role="dialog"
  >
    <header>
      <div>
        <p>Command Palette</p>
        <h2>{activeSession ? `Session ${activeSession}` : "No session attached"}</h2>
      </div>
      <button onclick={onClose} type="button">Close</button>
    </header>

    <input
      bind:this={inputElement}
      bind:value={query}
      oninput={(event) => onQueryChange((event.currentTarget as HTMLInputElement).value)}
      placeholder="Type /schedule list or search commands"
      type="text"
    />

    <ul>
      {#each items as item (item.command)}
        <li>
          <button onclick={() => onSelect(item.command)} type="button">
            <strong>{item.title ?? item.command}</strong>
            {#if item.title}
              <code>{item.command}</code>
            {/if}
            <span>{item.description}</span>
          </button>
        </li>
      {/each}
    </ul>
  </div>
{/if}

<style>
  .palette-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(1, 7, 12, 0.62);
    backdrop-filter: blur(6px);
    z-index: 40;
  }

  .palette-dialog {
    position: fixed;
    inset: 10vh auto auto 50%;
    transform: translateX(-50%);
    width: min(92vw, 42rem);
    display: grid;
    gap: 0.9rem;
    border: 1px solid rgba(145, 181, 221, 0.16);
    border-radius: 1.2rem;
    background: rgba(4, 13, 22, 0.96);
    box-shadow: 0 1.5rem 4rem rgba(0, 0, 0, 0.45);
    padding: 1rem;
    z-index: 41;
  }

  header {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
  }

  header p {
    margin: 0;
    color: var(--accent-strong);
    font-size: 0.76rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  header h2 {
    margin: 0.15rem 0 0;
    font-size: 1rem;
  }

  header button,
  li button {
    border: 0;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
  }

  input {
    width: 100%;
    border: 1px solid rgba(145, 181, 221, 0.18);
    border-radius: 0.85rem;
    background: rgba(7, 19, 31, 0.92);
    color: var(--text);
    font: inherit;
    padding: 0.85rem 0.95rem;
  }

  ul {
    display: grid;
    gap: 0.65rem;
    list-style: none;
    margin: 0;
    max-height: 22rem;
    overflow: auto;
    padding: 0;
  }

  li button {
    width: 100%;
    display: grid;
    gap: 0.25rem;
    border: 1px solid rgba(145, 181, 221, 0.12);
    border-radius: 0.9rem;
    background: rgba(7, 19, 31, 0.62);
    padding: 0.8rem;
    text-align: left;
  }

  li strong {
    color: var(--text);
  }

  li span {
    color: var(--muted);
    line-height: 1.4;
  }

  li code {
    color: var(--accent-strong);
    font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
    font-size: 0.8rem;
  }

  @media (max-width: 700px) {
    header button {
      min-height: 2.75rem;
      padding: 0.65rem 0.95rem;
    }
  }
</style>
