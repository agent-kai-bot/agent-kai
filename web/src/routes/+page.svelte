<script lang="ts">
  import { landingCards } from "$lib/web-shell";
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
      consumes the same websocket protocol.
    </p>
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
