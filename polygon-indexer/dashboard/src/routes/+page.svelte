<script>
  import { onMount } from 'svelte';

  import BlockTape from '$components/BlockTape.svelte';
  import ChainPulseBar from '$components/ChainPulseBar.svelte';
  import GasArc from '$components/GasArc.svelte';
  import HeroWhaleFeed from '$components/HeroWhaleFeed.svelte';
  import SystemSummary from '$components/SystemSummary.svelte';
  import TokenInspectorDrawer from '$components/TokenInspectorDrawer.svelte';
  import TokenRail from '$components/TokenRail.svelte';
  import { dashboard } from '$lib/stores/dashboard';

  onMount(() => {
    dashboard.bootstrap();
    return () => dashboard.disconnect();
  });
</script>

<svelte:head>
  <title>Polygon Chain Intelligence Dashboard</title>
  <meta
    content="Live Polygon chain pulse, block tape, whale flow, and token drill-down intelligence."
    name="description"
  />
</svelte:head>

<div class="min-h-screen text-slate-100">
  <ChainPulseBar connected={$dashboard.connected} overview={$dashboard.overview} />
  <BlockTape blocks={$dashboard.blocks} />

  <main class="mx-auto flex max-w-[1920px] flex-col gap-4 px-4 py-4 xl:px-6">
    {#if $dashboard.loading}
      <div class="panel-frame flex min-h-[420px] items-center justify-center text-sm text-slate-400">
        Loading Polygon dashboard surfaces…
      </div>
    {:else if $dashboard.error}
      <div class="panel-frame border-[rgba(255,77,141,0.22)] bg-[rgba(255,77,141,0.08)] px-5 py-5 text-sm text-slate-100">
        {$dashboard.error}
      </div>
    {:else}
      <div class="grid gap-4 xl:grid-cols-[300px_minmax(0,1fr)_340px] xl:items-start">
        <div class="order-2 xl:order-1">
          <TokenRail onSelect={dashboard.openToken} tokens={$dashboard.overview?.tokens ?? []} />
        </div>

        <div class="order-1 xl:order-2">
          <HeroWhaleFeed onSelectToken={dashboard.openToken} whales={$dashboard.whales} />
        </div>

        <div class="order-3 space-y-4 xl:order-3">
          <SystemSummary connected={$dashboard.connected} overview={$dashboard.overview} />
          <GasArc overview={$dashboard.overview} />
        </div>
      </div>
    {/if}
  </main>

  <TokenInspectorDrawer drawer={$dashboard.drawer} onClose={dashboard.closeToken} />
</div>
