<script>
  export let kicker = '';
  export let title = '';
  export let className = '';
</script>

<section class={`panel-frame p-4 ${className}`}>
  {#if kicker || title}
    <header class="panel-header">
      <div class="space-y-1">
        {#if kicker}
          <div class="panel-kicker">{kicker}</div>
        {/if}
        {#if title}
          <h2 class="panel-title">{title}</h2>
        {/if}
      </div>
      <slot name="meta" />
    </header>
  {/if}

  <slot />
</section>
