import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";

import AutoLoopBrainToggle from "$lib/components/AutoLoopBrainToggle.svelte";
import type { AutoLoopBrainConfig, AutoLoopBrainHealth } from "$lib/daemon/types";

function health(
  enabled: boolean,
  patch: Partial<AutoLoopBrainHealth> = {},
): AutoLoopBrainHealth {
  return {
    enabled,
    effective_client: "codex-cli",
    effective_model: "gpt-5.5",
    kill_switch_active: false,
    boot_probe_last_at: null,
    boot_probe_last_ok: null,
    calls_total: 0,
    escalations_total: 0,
    ...patch,
  };
}

function config(enabled: boolean): AutoLoopBrainConfig {
  return {
    enabled,
    client: "codex-cli",
    endpoint: null,
    model_id: "gpt-5.5",
    kill_switch_active: false,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("AutoLoopBrainToggle", () => {
  it("moves from idle to loading to enabled and back to idle", async () => {
    const initial = deferred<AutoLoopBrainHealth>();
    const toggle = deferred<AutoLoopBrainConfig>();
    const client = {
      fetchAutoLoopBrainHealth: vi
        .fn()
        .mockReturnValueOnce(initial.promise)
        .mockResolvedValueOnce(health(true)),
      updateAutoLoopBrainConfig: vi.fn().mockReturnValueOnce(toggle.promise),
    };

    const view = render(AutoLoopBrainToggle, {
      props: { client, token: "secret" },
    });

    expect(screen.getByRole("button").textContent).toContain("Loading");
    initial.resolve(health(false));
    const button = await screen.findByRole("button", { name: /Auto brain off/i });

    await fireEvent.click(button);
    expect(client.updateAutoLoopBrainConfig).toHaveBeenCalledWith(true, "secret");
    expect(button.getAttribute("aria-busy")).toBe("true");
    expect(button.textContent).toContain("Enabling");

    toggle.resolve(config(true));
    await screen.findByRole("button", { name: /Auto brain on/i });
    expect(screen.getByText("codex-cli · gpt-5.5")).toBeTruthy();
    expect(screen.getByRole("button").getAttribute("aria-busy")).toBe("false");

    view.unmount();
  });

  it("moves from idle to loading to error and reverts to the last server state", async () => {
    const toggle = deferred<AutoLoopBrainConfig>();
    const client = {
      fetchAutoLoopBrainHealth: vi.fn().mockResolvedValue(health(false)),
      updateAutoLoopBrainConfig: vi.fn().mockReturnValueOnce(toggle.promise),
    };

    const view = render(AutoLoopBrainToggle, {
      props: { client, token: "secret" },
    });

    const button = await screen.findByRole("button", { name: /Auto brain off/i });
    await fireEvent.click(button);
    expect(button.textContent).toContain("Enabling");

    toggle.reject(new Error("PATCH /api/daemon/config/auto_loop_brain failed (400)"));
    await screen.findByRole("alert");
    await waitFor(() => {
      expect(screen.getByRole("button").textContent).toContain("Auto brain off");
    });
    expect(screen.getByRole("alert").textContent).toContain("failed (400)");

    view.unmount();
  });

  it("disables the button when the kill switch is active", async () => {
    const client = {
      fetchAutoLoopBrainHealth: vi.fn().mockResolvedValue(
        health(false, {
          kill_switch_active: true,
        }),
      ),
      updateAutoLoopBrainConfig: vi.fn(),
    };

    const view = render(AutoLoopBrainToggle, {
      props: { client, token: "secret" },
    });

    const button = await screen.findByRole("button", {
      name: /kill switch active/i,
    });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(button);
    expect(client.updateAutoLoopBrainConfig).not.toHaveBeenCalled();

    view.unmount();
  });
});
