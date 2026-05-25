import { writable, type Writable } from "svelte/store";

export const FULLSCREEN_CHAT_STORAGE_KEY = "kai.chat.fullscreen";

export type FullscreenChatModeStore = Writable<boolean> & {
  toggle: () => void;
};

export function readStoredFullscreenChatMode(): boolean {
  if (typeof window === "undefined") {
    return false;
  }

  try {
    return (
      window.localStorage.getItem(FULLSCREEN_CHAT_STORAGE_KEY) === "true"
    );
  } catch {
    return false;
  }
}

export function writeStoredFullscreenChatMode(enabled: boolean): void {
  if (typeof window === "undefined") {
    return;
  }

  try {
    if (enabled) {
      window.localStorage.setItem(FULLSCREEN_CHAT_STORAGE_KEY, "true");
      return;
    }
    window.localStorage.removeItem(FULLSCREEN_CHAT_STORAGE_KEY);
  } catch {
    // Storage errors must not block the chat UI.
  }
}

export function createFullscreenChatModeStore(): FullscreenChatModeStore {
  const store = writable<boolean>(readStoredFullscreenChatMode());

  if (typeof window !== "undefined") {
    store.subscribe(writeStoredFullscreenChatMode);
  }

  return {
    subscribe: store.subscribe,
    set: store.set,
    update: store.update,
    toggle: () => {
      store.update((enabled) => !enabled);
    },
  };
}

export const fullscreenChatMode = createFullscreenChatModeStore();

export function toggleFullscreenChatMode(): void {
  fullscreenChatMode.toggle();
}
