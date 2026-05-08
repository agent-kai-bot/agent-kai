<script lang="ts">
  import { onDestroy, onMount } from "svelte";

  import { DaemonClient } from "$lib/daemon/client";
  import type { AutoLoopBrainConfig, AutoLoopBrainHealth } from "$lib/daemon/types";

  type AutoLoopBrainClient = Pick<
    DaemonClient,
    "fetchAutoLoopBrainHealth" | "updateAutoLoopBrainConfig"
  >;

  let {
    client = new DaemonClient(),
    token = "",
  }: {
    client?: AutoLoopBrainClient;
    token?: string;
  } = $props();

  let health = $state<AutoLoopBrainHealth | null>(null);
  let lastServerHealth = $state<AutoLoopBrainHealth | null>(null);
  let loading = $state(true);
  let toggling = $state(false);
  let errorMessage = $state("");
  let pollHandle: number | null = null;

  function visible(): boolean {
    return typeof document === "undefined" || document.visibilityState !== "hidden";
  }

  function safeError(error: unknown): string {
    if (error instanceof Error && error.message) {
      return error.message;
    }
    return "auto-loop-brain toggle failed";
  }

  async function refreshHealth(options: { silent?: boolean } = {}): Promise<void> {
    if (!visible()) {
      return;
    }
    if (!options.silent) {
      loading = true;
    }
    try {
      const next = await client.fetchAutoLoopBrainHealth(token);
      health = next;
      lastServerHealth = next;
      errorMessage = "";
    } catch (error) {
      if (!options.silent) {
        errorMessage = safeError(error);
      }
      if (lastServerHealth) {
        health = lastServerHealth;
      }
    } finally {
      if (!options.silent) {
        loading = false;
      }
    }
  }

  function startPolling(): void {
    if (typeof window === "undefined") {
      return;
    }
    stopPolling();
    pollHandle = window.setInterval(() => {
      void refreshHealth({ silent: true });
    }, 5000);
  }

  function stopPolling(): void {
    if (pollHandle !== null) {
      window.clearInterval(pollHandle);
      pollHandle = null;
    }
  }

  function applyConfig(config: AutoLoopBrainConfig): void {
    if (!health) {
      return;
    }
    health = {
      ...health,
      enabled: config.enabled,
      kill_switch_active: Boolean(config.kill_switch_active),
    };
  }

  async function toggleBrain(): Promise<void> {
    if (!health || health.kill_switch_active || toggling) {
      return;
    }
    const previous = lastServerHealth ?? health;
    const desired = !health.enabled;
    toggling = true;
    errorMessage = "";
    health = { ...health, enabled: desired };
    try {
      const config = await client.updateAutoLoopBrainConfig(desired, token);
      applyConfig(config);
      await refreshHealth({ silent: true });
    } catch (error) {
      health = previous;
      lastServerHealth = previous;
      errorMessage = safeError(error);
    } finally {
      toggling = false;
      loading = false;
    }
  }

  function buttonLabel(): string {
    if (health?.kill_switch_active) {
      return "kill switch active";
    }
    if (toggling) {
      return health?.enabled ? "Enabling..." : "Disabling...";
    }
    if (loading && !health) {
      return "Loading...";
    }
    return health?.enabled ? "Auto brain on" : "Auto brain off";
  }

  function caption(): string {
    if (!health) {
      return "auto-loop-brain";
    }
    return `${health.effective_client} · ${health.effective_model}`;
  }

  onMount(() => {
    const onVisibilityChange = () => {
      if (visible()) {
        void refreshHealth({ silent: true });
        startPolling();
      } else {
        stopPolling();
      }
    };
    void refreshHealth();
    startPolling();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      stopPolling();
    };
  });

  onDestroy(() => {
    stopPolling();
  });
</script>

<div class="auto-loop-brain-toggle" data-enabled={health?.enabled ? "true" : "false"}>
  <button
    aria-busy={toggling}
    aria-pressed={Boolean(health?.enabled)}
    class:enabled={Boolean(health?.enabled)}
    disabled={!health || toggling || health.kill_switch_active}
    onclick={() => void toggleBrain()}
    title={caption()}
    type="button"
  >
    {#if toggling}
      <span class="spinner" aria-hidden="true"></span>
    {/if}
    <span>{buttonLabel()}</span>
  </button>
  <span class="caption">{caption()}</span>
  {#if errorMessage}
    <span class="toggle-error" role="alert">{errorMessage}</span>
  {/if}
</div>

<style>
  .auto-loop-brain-toggle {
    display: grid;
    gap: 0.22rem;
    min-width: 8.5rem;
  }

  button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.45rem;
    min-height: 2.35rem;
    border: 1px solid rgba(145, 181, 221, 0.18);
    border-radius: 999px;
    background: rgba(7, 19, 31, 0.78);
    color: var(--text);
    cursor: pointer;
    font: inherit;
    font-size: 0.82rem;
    font-weight: 700;
    padding: 0.45rem 0.8rem;
    white-space: nowrap;
  }

  button.enabled {
    border-color: rgba(77, 211, 168, 0.38);
    background: rgba(77, 211, 168, 0.14);
    color: var(--text);
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.62;
  }

  .spinner {
    width: 0.72rem;
    height: 0.72rem;
    border: 2px solid rgba(242, 246, 251, 0.32);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }

  .caption,
  .toggle-error {
    min-width: 0;
    overflow-wrap: anywhere;
    font-size: 0.72rem;
    line-height: 1.25;
  }

  .caption {
    color: var(--muted);
  }

  .toggle-error {
    color: #ffb6b6;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
