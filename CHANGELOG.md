# Changelog

## 2026-04-09

### Fixed — cloud agent-k.ai endpoints now support tool calling

`d9fe09f` (the previous secrets-loading commit) noted that the cloud
`kai-fast` and `kai-smart` endpoints "are TEXT-ONLY — they accept
the `tools` field but ignore it." That note is now stale.

Root cause: the cloud gateway at `~/git/kai-new-v2` on `homedevbox`
(`192.168.222.222`) was the bottleneck, not the underlying vLLM. The
upstream model (`qwen35-gptq`) supports OpenAI-format function calling
natively — but four separate bugs in `backend/app/routers/v1_chat.py`
were stripping tool fields end to end:

1. `ChatCompletionRequest` Pydantic schema had no `tools` /
   `tool_choice` / `parallel_tool_calls` / `response_format` fields,
   so they were dropped on parse.
2. `ChatMessage` schema had `content: str` (not Optional) and no
   `tool_calls` / `tool_call_id` / `function_call`, breaking
   multi-turn conversations that round-trip a tool result.
3. `_build_upstream_kwargs()` didn't forward any of the tool fields
   even if they had survived the schema.
4. The non-streaming response builder hand-rolled
   `{"role": "assistant", "content": answer}` which silently
   dropped any `tool_calls` the upstream model returned. (The
   streaming path was fine — it forwarded chunks verbatim.)

A 91-line patch landed in `kai-new-v2/backend/app/routers/v1_chat.py`
that:
- adds the missing schema fields with `Optional[Any]` so we don't have
  to track minor OpenAI spec changes,
- forwards tools/tool_choice/parallel_tool_calls/response_format
  verbatim through `_build_upstream_kwargs`,
- serializes the upstream message via `choice.message.model_dump`
  in the non-streaming response builder so every field round-trips.

Verified live: direct REST + LangChain ChatOpenAI + full sub-agent
end-to-end all return `tool_calls` correctly with `finish_reason:
tool_calls` and accurate usage metering.

### Changed — `kai-smart` is the default primary for every agent

The open source agent now defaults to the cloud `agent-k.ai` API
(`kai-smart`) as the primary endpoint for every agent. Local vLLM
and Codex CLI are the fallbacks for power users who prefer
self-hosted or who already have a ChatGPT subscription.

This is the default to drive traffic to the project's hosted API —
which is how the project is monetized. Users get a working agent
out of the box (just drop `AGENT-KAI-API-KEY.txt` in the project
root or set the `AGENT_KAI_API_KEY` env var) and the inference cost
is debited from their custodial KAI balance per actual token usage.
Power users who want to self-host can swap the primary by editing
`agent-config.json` or with `/model AGENT kai-local/qwen35-gptq`
at runtime — the multi-endpoint registry makes that a one-line
override per agent.

| Agent         | Primary    | Fallback chain                                |
|---------------|------------|-----------------------------------------------|
| nano (main)   | kai-smart  | → kai-local/qwen35-gptq → codex-cli/gpt-5.4   |
| analyst       | kai-smart  | → kai-local/qwen35-gptq → codex-cli/gpt-5.4   |
| trader        | kai-smart  | → kai-local/qwen35-gptq → codex-cli/gpt-5.4   |
| risk-manager  | kai-smart  | → kai-local/qwen35-gptq → codex-cli/gpt-5.4   |
| mentor        | kai-smart  | → codex-cli/gpt-5.4 → kai-local/qwen35-gptq   |

Three-deep fallback gives every agent two independent recovery
paths: self-hosted vLLM (kai-local) and ChatGPT subscription
(codex-cli), either of which can pick up the slack if the cloud
endpoint is unreachable.

### Cross-repo note

The gateway fix lives in a separate repository (`~/git/kai-new-v2`
on `homedevbox`, served at `agent-k.ai/v1`). It is hot-patched in
the running `kai-new-v2-backend-1` docker container AND the on-disk
file is updated, so a future `docker compose build backend` will
include the fix in the image. The fix is NOT yet committed in
that repo because there is unrelated work-in-progress on that
machine — that's a separate decision for the repo owner.
