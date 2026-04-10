export const DAEMON_TOKEN_STORAGE_KEY = "kai.daemon.token";

export function readStoredToken(storageKey = DAEMON_TOKEN_STORAGE_KEY): string {
  if (typeof window === "undefined") {
    return "";
  }
  try {
    return window.localStorage.getItem(storageKey)?.trim() ?? "";
  } catch {
    return "";
  }
}

export function writeStoredToken(
  token: string,
  storageKey = DAEMON_TOKEN_STORAGE_KEY,
): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    const normalized = token.trim();
    if (normalized) {
      window.localStorage.setItem(storageKey, normalized);
    } else {
      window.localStorage.removeItem(storageKey);
    }
  } catch {
    // Ignore storage failures so private browsing or quota limits do not block the UI.
  }
}
