export type CommandPaletteItem = {
  command: string;
  description: string;
  title?: string;
  sample?: string;
};

export const PALETTE_ITEMS: CommandPaletteItem[] = [
  {
    command: "/chart full",
    title: "Chart: Full",
    description: "Reset the center column to the default chart-first split.",
  },
  {
    command: "/chart half",
    title: "Chart: Half",
    description: "Split the chart and chat panels evenly in the center column.",
  },
  {
    command: "/chart mini",
    title: "Chart: Mini",
    description: "Shrink the chart to a compact strip and give most of the height to chat.",
  },
  {
    command: "/chart hide",
    title: "Chart: Hidden",
    description: "Collapse the chart into a slim status bar and dedicate the column to chat.",
  },
  {
    command: "/status",
    description: "Summarize the current session state.",
  },
  {
    command: "/schedule list",
    description: "Show active scheduled jobs for this session.",
  },
  {
    command: "/schedule pause all",
    description: "Pause all scheduled jobs owned by this session.",
  },
  {
    command: "/schedule list all",
    description: "Inspect active scheduled jobs across sessions.",
  },
  {
    command: "/sessions",
    description: "Ask the daemon for available sessions.",
  },
  {
    command: "/chart BTC 1h",
    description: "Switch the chart to BTC on the 1h timeframe.",
  },
];

export function filterPaletteItems(query: string): CommandPaletteItem[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) {
    return PALETTE_ITEMS;
  }
  return PALETTE_ITEMS.filter((item) => {
    const haystack = `${item.title ?? ""} ${item.command} ${item.description}`.toLowerCase();
    return haystack.includes(normalized);
  });
}

export function resolvePaletteQuery(
  query: string,
  items: CommandPaletteItem[],
): string {
  const normalized = query.trim();
  if (!normalized) {
    return items[0]?.command ?? "";
  }
  if (normalized.startsWith("/")) {
    return normalized;
  }
  return items[0]?.command ?? normalized;
}

export function splitSlashInput(raw: string): { command: string; args: string } {
  const normalized = raw.trim();
  if (!normalized) {
    return { command: "", args: "" };
  }
  const prefixed = normalized.startsWith("/") ? normalized : `/${normalized}`;
  const firstSpace = prefixed.indexOf(" ");
  if (firstSpace === -1) {
    return { command: prefixed, args: "" };
  }
  return {
    command: prefixed.slice(0, firstSpace),
    args: prefixed.slice(firstSpace + 1).trim(),
  };
}
