<script lang="ts">
  import Panel from "$lib/components/Panel.svelte";

  export type EventRow = {
    headline: string;
    detail: string;
    tone?: "neutral" | "positive" | "warning" | "danger";
  };

  let {
    eyebrow = "",
    title,
    subtitle = "",
    emptyMessage,
    items,
    mobileCollapsible = false,
    initiallyOpen = true,
  }: {
    eyebrow?: string;
    title: string;
    subtitle?: string;
    emptyMessage: string;
    items: EventRow[];
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
  } = $props();
</script>

<Panel
  {eyebrow}
  {title}
  {subtitle}
  initiallyOpen={initiallyOpen}
  mobileCollapsible={mobileCollapsible}
>
  {#if items.length}
    <ul class="events">
      {#each items as item, index (item.headline + item.detail + index)}
        <li class={item.tone ?? "neutral"}>
          <strong>{item.headline}</strong>
          <p>{item.detail}</p>
        </li>
      {/each}
    </ul>
  {:else}
    <p class="empty">{emptyMessage}</p>
  {/if}
</Panel>

<style>
  .events {
    display: grid;
    gap: 0.65rem;
    list-style: none;
    margin: 0;
    padding: 0;
    min-height: 0;
  }

  .events li {
    border: 1px solid rgba(145, 181, 221, 0.08);
    border-radius: 0.9rem;
    background: rgba(7, 19, 31, 0.68);
    padding: 0.8rem;
  }

  .events li.positive {
    border-color: rgba(77, 211, 168, 0.2);
  }

  .events li.warning {
    border-color: rgba(243, 196, 106, 0.2);
  }

  .events li.danger {
    border-color: rgba(255, 143, 143, 0.24);
  }

  .events strong {
    display: block;
    margin-bottom: 0.3rem;
  }

  .events p,
  .empty {
    margin: 0;
    color: var(--muted);
    line-height: 1.5;
  }
</style>
