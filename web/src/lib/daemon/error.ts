import type { ErrorEnvelope } from "$lib/daemon/types";

export function formatDaemonError(envelope: ErrorEnvelope): string {
  const message = envelope.message.trim();
  const errorClass = envelope.error_class?.trim();
  const errorMessage = envelope.error_message?.trim();
  const hint = envelope.actionable_hint?.trim();

  if (!errorClass && !errorMessage && !hint) {
    return message;
  }

  const lines: string[] = [];
  const headline =
    message ||
    [errorClass ? `[${errorClass}]` : "", errorMessage ?? ""]
      .filter(Boolean)
      .join(" ");
  if (headline) {
    lines.push(headline);
  }
  if (errorMessage && !headline.includes(errorMessage)) {
    lines.push(errorMessage);
  }
  if (hint && !headline.includes(hint)) {
    lines.push(`Hint: ${hint}`);
  }
  return lines.join("\n");
}
