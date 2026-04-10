<script lang="ts">
  let {
    title,
    eyebrow = "",
    subtitle = "",
    children,
  }: {
    title: string;
    eyebrow?: string;
    subtitle?: string;
    children?: import("svelte").Snippet;
  } = $props();
</script>

<section class="panel-frame">
  <header class="panel-header">
    <div>
      {#if eyebrow}
        <p>{eyebrow}</p>
      {/if}
      <h2>{title}</h2>
      {#if subtitle}
        <span>{subtitle}</span>
      {/if}
    </div>
  </header>

  <div class="panel-body">
    {#if children}
      {@render children()}
    {/if}
  </div>
</section>

<style>
  .panel-frame {
    display: grid;
    gap: 0.9rem;
    border: 1px solid rgba(145, 181, 221, 0.14);
    border-radius: 1rem;
    background: rgba(5, 17, 28, 0.46);
    padding: 1rem;
    min-height: 0;
  }

  .panel-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }

  .panel-header p,
  .panel-header span {
    margin: 0;
    color: var(--muted);
    line-height: 1.4;
  }

  .panel-header p {
    color: var(--accent-strong);
    font-size: 0.76rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .panel-header h2 {
    margin: 0.15rem 0 0;
    font-size: 1rem;
  }

  .panel-body {
    min-height: 0;
  }
</style>
