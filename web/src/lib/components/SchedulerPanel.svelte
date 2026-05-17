<script lang="ts">
  import Panel from "$lib/components/Panel.svelte";
  import type { ScheduledJobRow } from "$lib/daemon/types";

  let {
    jobs,
    subtitle = "",
    emptyMessage = "No scheduled jobs found.",
    mobileCollapsible = false,
    initiallyOpen = true,
  }: {
    jobs: ScheduledJobRow[];
    subtitle?: string;
    emptyMessage?: string;
    mobileCollapsible?: boolean;
    initiallyOpen?: boolean;
  } = $props();

  function valueOrDash(value: string | number | null | undefined): string {
    if (value === null || value === undefined || value === "") {
      return "-";
    }
    return String(value);
  }

  function runCount(job: ScheduledJobRow): string {
    return String(job.run_count ?? 0);
  }
</script>

<Panel
  eyebrow="Scheduler"
  title="Scheduled Jobs"
  {subtitle}
  initiallyOpen={initiallyOpen}
  mobileCollapsible={mobileCollapsible}
>
  {#if jobs.length}
    <div class="scheduler-table-wrap">
      <table class="scheduler-table">
        <thead>
          <tr>
            <th>Id</th>
            <th>Session</th>
            <th>Status</th>
            <th>Cron</th>
            <th>Next Run</th>
            <th>Last Run</th>
            <th>Runs</th>
            <th>Max</th>
            <th>Prompt</th>
          </tr>
        </thead>
        <tbody>
          {#each jobs as job (job.id)}
            <tr>
              <td class="mono" title={job.id}>{job.id}</td>
              <td>{job.owner_session}</td>
              <td><span class={`status ${job.status}`}>{job.status}</span></td>
              <td class="mono">{valueOrDash(job.cron ?? job.schedule)}</td>
              <td class="mono" title={valueOrDash(job.next_run)}>{valueOrDash(job.next_run)}</td>
              <td class="mono" title={valueOrDash(job.last_run)}>{valueOrDash(job.last_run)}</td>
              <td>{runCount(job)}</td>
              <td>{valueOrDash(job.max_runs)}</td>
              <td class="prompt" title={job.prompt_preview}>{job.prompt_preview}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <p class="empty">{emptyMessage}</p>
  {/if}
</Panel>

<style>
  .scheduler-table-wrap {
    min-height: 0;
    overflow: auto;
  }

  .scheduler-table {
    width: 100%;
    min-width: 58rem;
    border-collapse: collapse;
    font-size: 0.78rem;
  }

  th,
  td {
    border-bottom: 1px solid rgba(145, 181, 221, 0.1);
    padding: 0.55rem 0.5rem;
    text-align: left;
    vertical-align: top;
  }

  th {
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  td {
    color: var(--text);
    line-height: 1.35;
  }

  .mono {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    max-width: 11rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .prompt {
    min-width: 16rem;
    max-width: 22rem;
    color: var(--muted);
  }

  .status {
    display: inline-flex;
    align-items: center;
    min-height: 1.45rem;
    border: 1px solid rgba(145, 181, 221, 0.14);
    border-radius: 0.45rem;
    padding: 0.1rem 0.45rem;
    color: var(--muted);
    background: rgba(7, 19, 31, 0.68);
  }

  .status.active {
    border-color: rgba(77, 211, 168, 0.24);
    color: var(--accent-strong);
  }

  .status.failed {
    border-color: rgba(255, 143, 143, 0.28);
    color: #ffb1b1;
  }

  .empty {
    margin: 0;
    color: var(--muted);
    line-height: 1.5;
  }
</style>
