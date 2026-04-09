"""LangChain agent core — AgentRunner wrapping AgentExecutor with fallback."""

from pathlib import Path
from typing import AsyncIterator

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI

from agent.memory_store import MemoryStore
from agent.memory_tool import create_memory_tool
from agent.prompts import SYSTEM_PROMPT, build_main_system_prompt
from agent.runtime_utils import EMPTY_RESPONSE_ERROR, ensure_non_empty_response
from agent.skills_store import SkillStore
from agent.skills_tool import create_skills_tools
from agent_logger import (
    get_logger,
    log_agent_event,
    log_llm_request,
    log_llm_response,
    log_tool_call,
)
from config import (
    ENDPOINTS,
    MEMORY_CHAR_LIMIT,
    MEMORY_ENABLED,
    SKILLS_ENABLED,
    USER_CHAR_LIMIT,
    USER_PROFILE_ENABLED,
    get_agent_config,
    get_endpoint,
    get_memory_path,
    get_skills_dir,
    get_user_profile_path,
)


def create_llm(endpoint_cfg=None):
    """Build a chat model instance from an endpoint config dict.

    Routes by ``provider`` field on the config:

    - ``codex-cli`` (or any ``base_url`` ending in chatgpt.com/codex):
      uses ``ChatCodex``, a small subclass of ``ChatOpenAI`` that loads
      OAuth credentials from ``~/.codex/auth.json`` and routes any
      system message into the Responses API ``instructions`` field.

    - everything else: standard ``ChatOpenAI`` against an
      OpenAI-compatible endpoint (the existing path for vLLM, the
      cloud kai-* endpoints, OpenAI direct, OpenRouter, etc.).
    """
    if endpoint_cfg is None:
        first = next(iter(ENDPOINTS), None)
        endpoint_cfg = get_endpoint(first) if first else {}

    provider = (endpoint_cfg.get("provider") or "").lower()
    base_url = endpoint_cfg.get("base_url") or ""
    if provider == "codex-cli" or "chatgpt.com" in base_url:
        return _create_codex_chat_model(endpoint_cfg)

    return ChatOpenAI(
        base_url=endpoint_cfg["base_url"],
        api_key=endpoint_cfg.get("api_key", "not-needed"),
        model=endpoint_cfg["model"],
        temperature=endpoint_cfg.get("temperature", 0.6),
        top_p=endpoint_cfg.get("top_p", 0.95),
        max_tokens=endpoint_cfg.get("max_tokens", 4096),
        streaming=True,
    )


def _create_codex_chat_model(endpoint_cfg: dict):
    """Build a Codex Responses chat model bound to the user's ChatGPT subscription.

    Loads (and refreshes if necessary) the OAuth credentials the
    codex CLI stores at ``~/.codex/auth.json`` — the user must have
    run ``codex login`` once OR ``python -m agent.codex_auth login``
    OR called ``agent.codex_auth.login()`` from the TUI.

    Returns a ``ChatCodex`` instance configured to talk to the
    Codex Responses endpoint with the proper auth headers and
    request body shape.
    """
    from agent.codex_auth import get_valid_credentials, DEFAULT_AUTH_PATH

    creds = get_valid_credentials()
    if creds is None:
        raise RuntimeError(
            "Codex endpoint requires OAuth credentials. Run "
            "`codex login` (the official CLI) or "
            "`python -m agent.codex_auth login` to authenticate, "
            f"then ensure {DEFAULT_AUTH_PATH} exists."
        )

    base_url = endpoint_cfg.get("base_url") or "https://chatgpt.com/backend-api/codex"
    model = endpoint_cfg.get("model") or "gpt-5.4"
    reasoning_effort = endpoint_cfg.get("reasoning_effort", "medium")
    text_verbosity = endpoint_cfg.get("text_verbosity", "medium")

    extra_body = {
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "text": {"verbosity": text_verbosity},
        "reasoning": {"effort": reasoning_effort, "summary": "auto"},
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    return ChatCodex(
        base_url=base_url,
        api_key=creds.access_token,
        model=model,
        default_headers={
            "chatgpt-account-id": creds.account_id,
            "originator": "kai",
            "User-Agent": "kai-agent (linux)",
            "OpenAI-Beta": "responses=experimental",
        },
        use_responses_api=True,
        streaming=True,  # Codex Responses requires stream=true
        extra_body=extra_body,
    )


class ChatCodex(ChatOpenAI):
    """ChatOpenAI subclass that adapts to Codex's Responses API quirks.

    Two adjustments are needed on top of vanilla
    ``ChatOpenAI(use_responses_api=True)``:

    1. Codex requires ``instructions`` to be set on every request.
       LangChain's default behavior would send the system message
       as the first ``input`` entry, which Codex rejects with
       "Instructions are required". This subclass intercepts the
       payload, pulls any system message out of ``input``, and
       moves its text into the top-level ``instructions`` field.

    2. ``store=false`` and ``stream=true`` are required (Codex
       refuses any other combination). Both are pre-set via
       ``extra_body`` and ``streaming=True`` in ``_create_codex_chat_model``.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        return _move_system_to_instructions(payload)


def _move_system_to_instructions(payload: dict) -> dict:
    """Pull system messages out of ``input`` and into ``instructions``.

    LangChain's responses API path puts everything in ``input``. Codex
    needs the system content as a top-level ``instructions`` string.
    Mutates and returns the payload.
    """
    inp = payload.get("input")
    if not isinstance(inp, list):
        return payload

    sys_chunks: list[str] = []
    remaining: list = []
    for msg in inp:
        if not isinstance(msg, dict) or msg.get("role") != "system":
            remaining.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, str):
            sys_chunks.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    text = c.get("text") or c.get("input_text") or ""
                    if text:
                        sys_chunks.append(text)
                elif isinstance(c, str):
                    sys_chunks.append(c)

    if sys_chunks:
        existing = payload.get("instructions") or ""
        merged = "\n\n".join([existing, *sys_chunks]) if existing else "\n\n".join(sys_chunks)
        payload["instructions"] = merged
        payload["input"] = remaining
    return payload


def create_prompt(system_prompt=None):
    """Create the chat prompt template."""
    # Escape curly braces in system prompt so LangChain doesn't treat them as template vars
    prompt_text = (system_prompt or SYSTEM_PROMPT).replace("{", "{{").replace("}", "}}")
    return ChatPromptTemplate.from_messages([
        ("system", prompt_text),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])


def build_agent_memory_store(agent_name: str) -> MemoryStore | None:
    """Construct and load a MemoryStore for the given agent.

    Returns None if memory is globally disabled in config. The store
    is always loaded from disk here (not lazily) so the frozen
    system-prompt snapshot is captured before the system prompt is
    built — load order matters.
    """
    if not MEMORY_ENABLED and not USER_PROFILE_ENABLED:
        return None
    store = MemoryStore(
        memory_path=Path(get_memory_path(agent_name)),
        user_path=Path(get_user_profile_path()),
        memory_char_limit=MEMORY_CHAR_LIMIT,
        user_char_limit=USER_CHAR_LIMIT,
    )
    try:
        store.load_from_disk()
    except Exception as exc:  # noqa: BLE001
        # A corrupt MEMORY.md should never crash agent startup.
        # Log and continue with an empty store.
        import logging
        logging.getLogger(__name__).warning(
            "memory: load_from_disk failed for %s: %s", agent_name, exc
        )
    return store


def render_memory_block(store: MemoryStore | None) -> str:
    """Render the combined MEMORY.md + USER.md block for a system prompt.

    Respects the individual ``MEMORY_ENABLED`` / ``USER_PROFILE_ENABLED``
    flags so either store can be disabled independently. Returns an
    empty string if there's nothing to inject.
    """
    if store is None:
        return ""
    parts: list[str] = []
    if MEMORY_ENABLED:
        mem_block = store.format_for_system_prompt("memory")
        if mem_block:
            parts.append(mem_block)
    if USER_PROFILE_ENABLED:
        user_block = store.format_for_system_prompt("user")
        if user_block:
            parts.append(user_block)
    return "\n\n".join(parts)


def build_agent_skill_store(agent_name: str) -> SkillStore | None:
    """Construct a SkillStore pointing at the agent's own skills dir.

    Returns None if skills are globally disabled. The store itself is
    lazy — directories are created on first write, not here.
    """
    if not SKILLS_ENABLED:
        return None
    return SkillStore(skills_dir=Path(get_skills_dir(agent_name)))


def render_skill_catalog(store: SkillStore | None) -> str:
    """Render a short skill catalog for injection into the system prompt.

    Intentionally minimal — just name + one-line description. The
    agent uses ``skill_view`` to load the body of any skill it wants
    to follow, so we don't dump full content here. Returns empty
    string if there are no skills yet, to avoid noise in the prompt.
    """
    if store is None:
        return ""
    try:
        items = store.list_skills()
    except Exception:  # noqa: BLE001
        return ""
    if not items:
        return ""
    lines = [
        "══════════════════════════════════════════════",
        f"SKILLS (your reusable recipes) [{len(items)} available]",
        "══════════════════════════════════════════════",
        "Call skill_view(name) to read the full body of any skill below.",
        "",
    ]
    for item in items:
        name = item.get("name", "?")
        desc = item.get("description", "")
        category = item.get("category", "")
        suffix = f" ({category})" if category else ""
        lines.append(f"- {name}{suffix}: {desc}")
    return "\n".join(lines)


class AgentRunner:
    """Manages the LangChain agent with primary/fallback LLM endpoints."""

    def __init__(self, tools, bus=None, agent_name=None):
        self.bus = bus
        self.tools = list(tools)  # copy so we can append the memory tool
        self.chat_history = []
        self.agent_name = agent_name or "kai"
        self.log = get_logger(self.agent_name)

        cfg = get_agent_config(agent_name) if agent_name else {}
        ep = cfg.get("endpoint")
        # Full chain — first failure walks the list in order
        fallback_chain = cfg.get("fallback_endpoints") or []
        # Backwards-compat alias preserved for any external readers
        self.fallback_endpoint = cfg.get("fallback_endpoint")
        # Persisted so reload_llm() can rebuild the executors
        # without rerunning all of __init__
        self._max_iterations = cfg.get("max_iterations", 200)
        max_iterations = self._max_iterations
        system_prompt = cfg.get("system_prompt")

        self.llm = create_llm(ep)

        # Load persistent memory and register the tool that mutates it.
        # The frozen snapshot is injected into the system prompt here;
        # mid-session writes through the tool update disk + the tool's
        # live state view but never the system prompt (preserves prefix
        # cache and keeps the LLM's notion of "what's in memory" stable
        # for the rest of the turn).
        self.memory_store = build_agent_memory_store(self.agent_name)
        memory_block = render_memory_block(self.memory_store)
        self.tools.append(create_memory_tool(self.memory_store))

        # Load skills and register the three skill tools. Skills are
        # procedural memory — on-demand recipes the agent authored in
        # previous sessions. A minimal catalog is injected into the
        # system prompt so the LLM knows what's on the shelf without
        # paying the token cost of full content; bodies are loaded
        # via the skill_view tool only when needed.
        self.skill_store = build_agent_skill_store(self.agent_name)
        skill_catalog = render_skill_catalog(self.skill_store)
        self.tools.extend(create_skills_tools(self.skill_store))

        # Compose both memory + skill catalog into the shared prompt
        # "identity" section. Join with a blank line so the headers
        # stay visually distinct.
        identity_block = "\n\n".join(b for b in (memory_block, skill_catalog) if b)

        # Persist the assembled prompt so reload_llm() can reuse it
        # without re-loading memory + skills (which is expensive
        # and would also reset the frozen system-prompt snapshot
        # the LLM has been working with mid-session).
        self._prompt = create_prompt(
            build_main_system_prompt(system_prompt, memory_block=identity_block)
        )
        prompt = self._prompt
        agent = create_tool_calling_agent(self.llm, self.tools, prompt)
        self.executor = AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=False,
            max_iterations=max_iterations,
            handle_parsing_errors=True,
        )

        # Build executors for each fallback in the chain. Skip any
        # that fail to initialize (e.g. an unreachable endpoint or
        # missing credentials) so a broken fallback never blocks
        # agent startup.
        self.fallback_executors: list[AgentExecutor] = []
        for fb_cfg in fallback_chain:
            try:
                fb_llm = create_llm(fb_cfg)
                fb_agent = create_tool_calling_agent(fb_llm, self.tools, prompt)
                self.fallback_executors.append(AgentExecutor(
                    agent=fb_agent,
                    tools=self.tools,
                    verbose=False,
                    max_iterations=max_iterations,
                    handle_parsing_errors=True,
                ))
            except Exception as exc:
                self.log.warning(
                    "fallback executor build failed for %s endpoint=%s: %s",
                    self.agent_name, fb_cfg.get("base_url"), exc,
                )
        # Backwards-compat alias for callers that read the singular
        self.fallback_executor = self.fallback_executors[0] if self.fallback_executors else None

        log_agent_event(self.agent_name, "init", {
            "endpoint": ep.get("base_url") if ep else None,
            "model": ep.get("model") if ep else None,
            "max_iterations": max_iterations,
            "fallback_chain": [(f.get("base_url"), f.get("model")) for f in fallback_chain],
            "tools": [t.name for t in self.tools],
        })

    def reload_llm(self) -> dict:
        """Re-read this agent's config and rebuild the LLM + executor in place.

        Used by the TUI's ``/model`` slash command to swap the main
        agent's endpoint at runtime without losing chat history,
        memory, skills, or any tool wiring. Reads the (presumably
        already-mutated) ``AGENTS`` dict via ``get_agent_config``,
        builds a fresh primary executor and fallback chain on top of
        the existing tools + prompt, then swaps the instance
        attributes atomically.

        Notes:
        - Tools are NOT rebuilt — the existing list (with memory +
          skills tools already appended) is reused.
        - The system prompt is NOT rebuilt — ``self._prompt`` is the
          frozen snapshot from __init__ time, which preserves the
          memory/skills identity block the LLM has been operating
          against. If the user wants the prompt regenerated they
          should restart the TUI.
        - Chat history is preserved (it's owned by the runner, not
          the executor).

        Returns a dict summarizing what got loaded — useful for
        chat-message feedback after the swap.
        """
        cfg = get_agent_config(self.agent_name) or {}
        ep = cfg.get("endpoint")
        fallback_chain = cfg.get("fallback_endpoints") or []
        max_iterations = cfg.get("max_iterations", self._max_iterations)
        self._max_iterations = max_iterations

        # Primary
        self.llm = create_llm(ep)
        primary_agent = create_tool_calling_agent(self.llm, self.tools, self._prompt)
        self.executor = AgentExecutor(
            agent=primary_agent,
            tools=self.tools,
            verbose=False,
            max_iterations=max_iterations,
            handle_parsing_errors=True,
        )

        # Fallback chain
        self.fallback_executors = []
        for fb_cfg in fallback_chain:
            try:
                fb_llm = create_llm(fb_cfg)
                fb_agent = create_tool_calling_agent(fb_llm, self.tools, self._prompt)
                self.fallback_executors.append(AgentExecutor(
                    agent=fb_agent,
                    tools=self.tools,
                    verbose=False,
                    max_iterations=max_iterations,
                    handle_parsing_errors=True,
                ))
            except Exception as exc:
                self.log.warning(
                    "reload_llm fallback build failed for %s endpoint=%s: %s",
                    self.agent_name, fb_cfg.get("base_url"), exc,
                )
        self.fallback_executor = self.fallback_executors[0] if self.fallback_executors else None
        self.fallback_endpoint = cfg.get("fallback_endpoint")

        log_agent_event(self.agent_name, "reload_llm", {
            "endpoint": ep.get("base_url") if ep else None,
            "model": ep.get("model") if ep else None,
            "fallback_chain": [(f.get("base_url"), f.get("model")) for f in fallback_chain],
        })

        return {
            "endpoint": ep.get("base_url") if ep else None,
            "model": ep.get("model") if ep else None,
            "provider": ep.get("provider") if ep else None,
            "fallback_count": len(self.fallback_executors),
        }

    async def run(self, user_input: str) -> AsyncIterator[dict]:
        """Stream agent events. Falls back to secondary endpoint on error."""
        self.chat_history.append(HumanMessage(content=user_input))
        self.log.info("USER_INPUT agent=%s input=%s", self.agent_name, user_input[:200])

        # Log the full prompt at DEBUG level
        log_llm_request(self.agent_name, self.chat_history, input=user_input)

        accumulated = ""
        final_text = ""
        primary_failed = False
        emitted_final = False

        try:
            async for event in self._stream_executor(self.executor, user_input):
                yield event
                if event["type"] == "token":
                    accumulated += event["data"]
                elif event["type"] == "final":
                    final_text = event["data"]
                    emitted_final = True
                elif event["type"] == "error":
                    primary_failed = True

            if not primary_failed and not (final_text or accumulated).strip():
                primary_failed = True
                self.log.warning("EMPTY_RESPONSE agent=%s endpoint=primary", self.agent_name)
                yield {"type": "error", "data": "Primary endpoint returned an empty response."}

        except Exception as e:
            primary_failed = True
            self.log.error("PRIMARY_FAILED agent=%s error=%s", self.agent_name, str(e))
            yield {"type": "error", "data": f"Primary endpoint failed: {e}"}

        # Walk the fallback chain. Each entry is tried in order until
        # one returns a non-empty, non-error result.
        attempt = 0
        for fb_executor in self.fallback_executors:
            if not primary_failed:
                break
            attempt += 1
            label = f"fallback_{attempt}"
            log_agent_event(self.agent_name, label)
            yield {
                "type": "status",
                "data": f"Falling back to endpoint #{attempt} of {len(self.fallback_executors)}...",
            }
            primary_failed = False
            accumulated = ""
            try:
                async for event in self._stream_executor(fb_executor, user_input):
                    yield event
                    if event["type"] == "token":
                        accumulated += event["data"]
                    elif event["type"] == "final":
                        final_text = event["data"]
                        emitted_final = True
                    elif event["type"] == "error":
                        primary_failed = True
                if not primary_failed and not (final_text or accumulated).strip():
                    primary_failed = True
                    self.log.warning("EMPTY_RESPONSE agent=%s endpoint=%s", self.agent_name, label)
                    yield {"type": "error", "data": f"Endpoint #{attempt} returned an empty response."}
            except Exception as e:
                primary_failed = True
                self.log.error("FALLBACK_FAILED agent=%s attempt=%d error=%s", self.agent_name, attempt, str(e))
                yield {"type": "error", "data": f"Endpoint #{attempt} failed: {e}"}
                final_text = f"Error: {e}"

        response_text = ensure_non_empty_response(final_text or accumulated)
        if response_text == EMPTY_RESPONSE_ERROR:
            self.log.warning("EMPTY_RESPONSE agent=%s endpoint=final", self.agent_name)
        if not emitted_final or not (final_text or accumulated).strip():
            yield {"type": "final", "data": response_text}
        self.chat_history.append(AIMessage(content=response_text))

        # Log the full response at DEBUG
        log_llm_response(self.agent_name, response_text)
        self.log.info("AGENT_RESPONSE agent=%s length=%d", self.agent_name, len(response_text))

        if self.bus:
            try:
                await self.bus.publish(
                    f"agent.{self.bus.agent_name}.response",
                    {"response": response_text, "input": user_input},
                )
            except Exception:
                pass

    async def _stream_executor(self, executor, user_input):
        """Stream events from an AgentExecutor."""
        async for event in executor.astream_events(
            {"input": user_input, "chat_history": self.chat_history},
            version="v2",
        ):
            kind = event["event"]

            if kind == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                if hasattr(chunk, "content") and chunk.content:
                    yield {"type": "token", "data": chunk.content}

            elif kind == "on_tool_start":
                tool_name = event.get("name", "?")
                tool_input = event["data"].get("input", {})
                log_tool_call(self.agent_name, tool_name, tool_input)
                yield {"type": "tool_start", "data": {"tool": tool_name, "input": tool_input}}

            elif kind == "on_tool_end":
                tool_name = event.get("name", "?")
                tool_output = event["data"].get("output", "")
                log_tool_call(self.agent_name, tool_name, {}, tool_output=str(tool_output))
                yield {"type": "tool_end", "data": {"tool": tool_name, "output": str(tool_output)}}

            elif kind == "on_chain_end" and event.get("name") == "AgentExecutor":
                output = event["data"].get("output", {})
                if isinstance(output, dict):
                    text = output.get("output", "")
                else:
                    text = str(output) if output else ""
                yield {"type": "final", "data": text}
