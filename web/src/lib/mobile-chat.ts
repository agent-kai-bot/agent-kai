import {
  applyChatActivityEnvelope,
  clearChatActivityState,
  emptyChatActivityState,
  startChatActivityTurn,
  type ChatActivityState,
} from "$lib/chat-activity";
import {
  DEFAULT_HTTP_BASE_URL,
  DEFAULT_SESSION_NAME,
} from "$lib/daemon/client";
import { formatDaemonError } from "$lib/daemon/error";
import type {
  ChatHistoryEntry,
  ServerEnvelope,
  SessionStateSnapshot,
} from "$lib/daemon/types";

export const MOBILE_BASE_URL_STORAGE_KEY = "kai.mobile.baseUrl";
export const MOBILE_SESSION_STORAGE_KEY = "kai.mobile.session";

export type MobileChatState = {
  attachedSession: string;
  messages: ChatHistoryEntry[];
  streamingReply: string;
  activity: ChatActivityState;
  status: string;
  queueDepth: number;
  error: string;
  historyLabel: string;
};

export type OutgoingMobileMessage =
  | {
      kind: "input";
      text: string;
    }
  | {
      kind: "slash";
      text: string;
      command: string;
      args: string;
    };

export function emptyMobileChatState(): MobileChatState {
  return {
    attachedSession: "",
    messages: [],
    streamingReply: "",
    activity: emptyChatActivityState(),
    status: "disconnected",
    queueDepth: 0,
    error: "",
    historyLabel: "0 messages",
  };
}

function messageCountLabel(count: number): string {
  return `${count} ${count === 1 ? "message" : "messages"}`;
}

export function mobileChatStateFromSnapshot(
  snapshot: SessionStateSnapshot,
  options: {
    session?: string;
    status?: string;
    queueDepth?: number;
  } = {},
): MobileChatState {
  const total = snapshot.chat_history_total ?? snapshot.chat_history.length;
  const omitted = snapshot.chat_history_omitted ?? 0;
  const historyLabel =
    omitted > 0
      ? `${snapshot.chat_history.length}/${total} recent messages`
      : messageCountLabel(total);

  return {
    attachedSession: options.session ?? DEFAULT_SESSION_NAME,
    messages: [...snapshot.chat_history],
    streamingReply: "",
    activity: clearChatActivityState(),
    status: options.status ?? snapshot.activity_status ?? "idle",
    queueDepth: options.queueDepth ?? 0,
    error: "",
    historyLabel,
  };
}

export function startMobileUserTurn(
  state: MobileChatState,
  text: string,
  now = new Date(),
): MobileChatState {
  const normalized = text.trim();
  if (!normalized) {
    return state;
  }
  const messages = [
    ...state.messages,
    {
      role: "human",
      content: normalized,
      ts: now.toISOString(),
    },
  ];
  return {
    ...state,
    messages,
    streamingReply: "",
    activity: startChatActivityTurn(now),
    error: "",
    historyLabel: messageCountLabel(messages.length),
  };
}

export function applyMobileEnvelope(
  state: MobileChatState,
  envelope: ServerEnvelope,
  now = new Date(),
): MobileChatState {
  if (envelope.type === "session_attached") {
    return mobileChatStateFromSnapshot(envelope.state, {
      session: envelope.session,
      status: state.status,
      queueDepth: state.queueDepth,
    });
  }

  if (envelope.type === "status") {
    return {
      ...state,
      status: envelope.activity,
      queueDepth: envelope.queue,
      activity: applyChatActivityEnvelope(state.activity, envelope, now),
    };
  }

  if (envelope.type === "token") {
    return {
      ...state,
      streamingReply: `${state.streamingReply}${envelope.text}`,
      activity: applyChatActivityEnvelope(state.activity, envelope, now),
    };
  }

  if (envelope.type === "final") {
    const messages = [
      ...state.messages,
      {
        role: "ai",
        content: envelope.text,
        ts: now.toISOString(),
      },
    ];
    return {
      ...state,
      messages,
      streamingReply: "",
      activity: applyChatActivityEnvelope(state.activity, envelope, now),
      historyLabel: messageCountLabel(messages.length),
    };
  }

  if (
    envelope.type === "tool_start" ||
    envelope.type === "tool_end" ||
    envelope.type === "auto_started" ||
    envelope.type === "auto_progress" ||
    envelope.type === "auto_stopped"
  ) {
    return {
      ...state,
      activity: applyChatActivityEnvelope(state.activity, envelope, now),
    };
  }

  if (envelope.type === "error") {
    return {
      ...state,
      error: formatDaemonError(envelope),
      activity: clearChatActivityState(),
    };
  }

  return state;
}

export function parseOutgoingMobileMessage(
  raw: string,
): OutgoingMobileMessage | null {
  const text = raw.trim();
  if (!text) {
    return null;
  }
  if (!text.startsWith("/")) {
    return { kind: "input", text };
  }

  const firstWhitespace = text.search(/\s/);
  if (firstWhitespace === -1) {
    return {
      kind: "slash",
      text,
      command: text,
      args: "",
    };
  }

  return {
    kind: "slash",
    text,
    command: text.slice(0, firstWhitespace),
    args: text.slice(firstWhitespace + 1).trim(),
  };
}

export function normalizeDaemonBaseUrl(
  raw: string,
  fallback = DEFAULT_HTTP_BASE_URL,
): string {
  const candidate = raw.trim() || fallback;
  const fallbackUrl = new URL(fallback);
  const source = candidate.includes("://")
    ? candidate
    : `${fallbackUrl.protocol}//${candidate}`;
  const url = new URL(source);
  return `${url.protocol}//${url.host}`;
}
