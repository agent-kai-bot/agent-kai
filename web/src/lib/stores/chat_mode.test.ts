import { get } from "svelte/store";

import {
  createFullscreenChatModeStore,
  FULLSCREEN_CHAT_STORAGE_KEY,
  readStoredFullscreenChatMode,
  writeStoredFullscreenChatMode,
} from "$lib/stores/chat_mode";

describe("fullscreen chat mode store", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("defaults to normal mode without stored state", () => {
    expect(readStoredFullscreenChatMode()).toBe(false);
  });

  it("reads and writes the persisted fullscreen flag", () => {
    writeStoredFullscreenChatMode(true);
    expect(window.localStorage.getItem(FULLSCREEN_CHAT_STORAGE_KEY)).toBe(
      "true",
    );
    expect(readStoredFullscreenChatMode()).toBe(true);

    writeStoredFullscreenChatMode(false);
    expect(window.localStorage.getItem(FULLSCREEN_CHAT_STORAGE_KEY)).toBeNull();
    expect(readStoredFullscreenChatMode()).toBe(false);
  });

  it("persists toggle changes", () => {
    const store = createFullscreenChatModeStore();

    expect(get(store)).toBe(false);
    store.toggle();

    expect(get(store)).toBe(true);
    expect(window.localStorage.getItem(FULLSCREEN_CHAT_STORAGE_KEY)).toBe(
      "true",
    );
  });
});
