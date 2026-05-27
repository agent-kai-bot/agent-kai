import { formatDaemonError } from "$lib/daemon/error";

describe("formatDaemonError", () => {
  it("renders typed daemon error fields with the underlying message", () => {
    const text = formatDaemonError({
      type: "error",
      code: "agent_error",
      message:
        "Primary endpoint failed [codex_transport_connection_error]: RuntimeError: Connection error.",
      error_class: "codex_transport_connection_error",
      error_message:
        "RuntimeError: Connection error.; caused by Error: DNS lookup failed for chatgpt.com",
      actionable_hint:
        "Check Codex/ChatGPT endpoint connectivity, DNS, and local network reachability, then retry.",
    });

    expect(text).toContain("codex_transport_connection_error");
    expect(text).toContain("DNS lookup failed for chatgpt.com");
    expect(text).not.toBe("Connection error.");
  });

  it("keeps legacy untyped errors unchanged", () => {
    expect(
      formatDaemonError({
        type: "error",
        code: "bad_request",
        message: "missing session",
      }),
    ).toBe("missing session");
  });
});
