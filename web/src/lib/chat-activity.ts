import type {
  AutoProgressEnvelope,
  AutoStartedEnvelope,
  AutoStoppedEnvelope,
  FinalEnvelope,
  StatusEnvelope,
  TokenEnvelope,
  ToolEndEnvelope,
  ToolStartEnvelope,
} from "$lib/daemon/types";

export type ToolActivity = {
  id: number;
  tool: string;
  argsPreview: string;
  state: "running" | "complete";
  elapsedMs: number | null;
  ok: boolean | null;
};

export type AutoActivityState = {
  readonly: boolean;
  iterationsTotal: number;
  iterationsRemaining: number;
  iterationsUsed: number;
  elapsedSeconds: number;
  status: "running" | "stopped";
  reason: string;
};

export type ChatActivityState = {
  active: boolean;
  startedAt: string | null;
  statusActivity: string;
  tools: ToolActivity[];
  auto: AutoActivityState | null;
  nextToolId: number;
};

export type ChatActivityEnvelope =
  | StatusEnvelope
  | TokenEnvelope
  | FinalEnvelope
  | ToolStartEnvelope
  | ToolEndEnvelope
  | AutoStartedEnvelope
  | AutoProgressEnvelope
  | AutoStoppedEnvelope;

const TOOL_ARGS_PREVIEW_LIMIT = 96;

export function emptyChatActivityState(): ChatActivityState {
  return {
    active: false,
    startedAt: null,
    statusActivity: "",
    tools: [],
    auto: null,
    nextToolId: 1,
  };
}

export function startChatActivityTurn(now = new Date()): ChatActivityState {
  return {
    ...emptyChatActivityState(),
    active: true,
    startedAt: now.toISOString(),
    statusActivity: "thinking...",
  };
}

export function clearChatActivityState(): ChatActivityState {
  return emptyChatActivityState();
}

export function formatToolElapsed(ms: number | null | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) {
    return "";
  }
  if (ms < 1000) {
    return `${Math.round(ms)}ms`;
  }
  const roundedSeconds = Math.round(ms / 1000);
  if (roundedSeconds <= 60) {
    if (ms < 10_000 && ms % 1000 !== 0) {
      const tenths = Math.round(ms / 100) / 10;
      return `${tenths.toFixed(tenths % 1 === 0 ? 0 : 1)}s`;
    }
    return `${roundedSeconds}s`;
  }
  const minutes = Math.floor(roundedSeconds / 60);
  const seconds = roundedSeconds % 60;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

export function formatAutoElapsed(seconds: number): string {
  const ms = Math.max(0, Math.round(seconds * 1000));
  return formatToolElapsed(ms) || "0ms";
}

export function formatToolArgsPreview(args: unknown): string {
  if (args === null || typeof args === "undefined") {
    return "";
  }
  let preview: string;
  if (typeof args === "string") {
    preview = args;
  } else {
    try {
      preview = JSON.stringify(args);
    } catch {
      preview = String(args);
    }
  }
  const compact = preview.replace(/\s+/g, " ").trim();
  if (compact.length <= TOOL_ARGS_PREVIEW_LIMIT) {
    return compact;
  }
  return `${compact.slice(0, TOOL_ARGS_PREVIEW_LIMIT - 1).trimEnd()}...`;
}

export function applyChatActivityEnvelope(
  state: ChatActivityState,
  envelope: ChatActivityEnvelope,
  now = new Date(),
): ChatActivityState {
  if (envelope.type === "final") {
    return clearChatActivityState();
  }

  if (envelope.type === "status") {
    if (!state.active && envelope.activity === "idle") {
      return state;
    }
    return {
      ...ensureActive(state, now),
      statusActivity: envelope.activity || "idle",
    };
  }

  if (envelope.type === "token") {
    return ensureActive(state, now);
  }

  if (envelope.type === "tool_start") {
    const activeState = ensureActive(state, now);
    return {
      ...activeState,
      tools: [
        ...activeState.tools,
        {
          id: activeState.nextToolId,
          tool: envelope.tool,
          argsPreview: formatToolArgsPreview(envelope.args),
          state: "running",
          elapsedMs: null,
          ok: null,
        },
      ],
      nextToolId: activeState.nextToolId + 1,
    };
  }

  if (envelope.type === "tool_end") {
    const activeState = ensureActive(state, now);
    const runningIndex = activeState.tools.findIndex(
      (tool) => tool.tool === envelope.tool && tool.state === "running",
    );
    if (runningIndex === -1) {
      return {
        ...activeState,
        tools: [
          ...activeState.tools,
          {
            id: activeState.nextToolId,
            tool: envelope.tool,
            argsPreview: "",
            state: "complete",
            elapsedMs: envelope.elapsed_ms ?? null,
            ok: envelope.ok,
          },
        ],
        nextToolId: activeState.nextToolId + 1,
      };
    }
    return {
      ...activeState,
      tools: activeState.tools.map((tool, index) =>
        index === runningIndex
          ? {
              ...tool,
              state: "complete",
              elapsedMs: envelope.elapsed_ms ?? null,
              ok: envelope.ok,
            }
          : tool,
      ),
    };
  }

  if (envelope.type === "auto_started" || envelope.type === "auto_progress") {
    const activeState = ensureActive(state, now);
    return {
      ...activeState,
      auto: autoStateFromEnvelope(envelope, "running"),
    };
  }

  if (envelope.type === "auto_stopped") {
    const activeState = ensureActive(state, now);
    return {
      ...activeState,
      auto: autoStateFromEnvelope(envelope, "stopped"),
    };
  }

  return state;
}

function ensureActive(state: ChatActivityState, now: Date): ChatActivityState {
  if (state.active) {
    return state;
  }
  return {
    ...state,
    active: true,
    startedAt: now.toISOString(),
  };
}

function autoStateFromEnvelope(
  envelope: AutoStartedEnvelope | AutoProgressEnvelope | AutoStoppedEnvelope,
  status: AutoActivityState["status"],
): AutoActivityState {
  return {
    readonly: envelope.readonly,
    iterationsTotal: envelope.iterations_total,
    iterationsRemaining: envelope.iterations_remaining,
    iterationsUsed: envelope.iterations_used,
    elapsedSeconds: envelope.elapsed_seconds,
    status,
    reason: "reason" in envelope ? envelope.reason : "",
  };
}
