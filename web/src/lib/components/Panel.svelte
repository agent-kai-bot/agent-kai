<script lang="ts">
  let {
    title,
    eyebrow = "",
    subtitle = "",
    mobileCollapsible = false,
    initiallyOpen = true,
    bodyScroll = true,
    children,
  }: {
    title: string;
    eyebrow?: string;
    subtitle?: string;
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
    bodyScroll?: boolean;
    children?: import("svelte").Snippet;
  } = $props();

  let expanded = $state(true);

  $effect(() => {
    expanded = initiallyOpen;
  });
</script>

<section
  class="panel-frame"
  class:body-static={!bodyScroll}
  class:is-collapsed={!expanded}
  class:mobile-collapsible={mobileCollapsible}
>
  <header class="panel-header">
    <div class="panel-copy">
      {#if eyebrow}
        <p>{eyebrow}</p>
      {/if}
      <h2>{title}</h2>
      {#if subtitle}
        <span>{subtitle}</span>
      {/if}
    </div>
    {#if mobileCollapsible}
      <button
        aria-expanded={expanded}
        class="panel-toggle"
        onclick={() => {
          expanded = !expanded;
        }}
        type="button"
      >
        {#if expanded}Hide{:else}Show{/if}
      </button>
    {/if}
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
    grid-template-rows: auto minmax(0, 1fr);
    gap: 0.9rem;
    border: 1px solid rgba(145, 181, 221, 0.14);
    border-radius: 1rem;
    background: rgba(5, 17, 28, 0.46);
    padding: 1rem;
    min-height: 0;
    height: 100%;
    overflow: hidden;
  }

  .panel-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }

  .panel-copy {
    min-width: 0;
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

  .panel-toggle {
    display: none;
    border: 1px solid rgba(145, 181, 221, 0.16);
    border-radius: 999px;
    background: rgba(7, 19, 31, 0.72);
    color: var(--text);
    cursor: pointer;
    font: inherit;
    font-size: 0.78rem;
    font-weight: 700;
    padding: 0.45rem 0.8rem;
  }

  .panel-body {
    min-height: 0;
    overflow: auto;
  }

  .panel-frame.body-static .panel-body {
    overflow: hidden;
  }

  @media (max-width: 700px) {
    .panel-frame {
      display: block;
      height: auto;
      overflow: visible;
    }

    .panel-body {
      min-height: auto;
      overflow: visible;
    }

    .panel-header {
      margin-bottom: 0.75rem;
    }

    .panel-frame.body-static .panel-body {
      overflow: visible;
    }

    .panel-toggle {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }

    .panel-frame.mobile-collapsible.is-collapsed {
      display: block;
    }

    .panel-frame.mobile-collapsible.is-collapsed .panel-header {
      margin-bottom: 0;
    }

    .panel-frame.mobile-collapsible.is-collapsed .panel-body {
      display: none;
    }
  }
</style>
