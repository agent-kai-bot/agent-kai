"""LangChain agent core — AgentRunner wrapping AgentExecutor with fallback."""

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Any, AsyncIterator

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI

from agent.auto_prompt import build_auto_suffix, parse_auto_state
from agent.memory_store import MemoryStore
from agent.memory_tool import create_memory_tool
from agent.prompts import SYSTEM_PROMPT, build_main_system_prompt
from agent.runtime_utils import EMPTY_RESPONSE_ERROR, ensure_non_empty_response
from agent.skills_store import SkillStore
from agent.skills_tool import create_skills_tools
from agent.tool_policy import get_tool_policy
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

DEFAULT_LLM_HISTORY_MAX_MESSAGES = 80
DEFAULT_LLM_HISTORY_MAX_CHARS = 80_000


def _env_int(name: str, default: int) -> int:
    """Return a positive integer from an environment variable.

    Args:
        name: Environment variable name.
        default: Fallback value when unset or invalid.

    Returns:
        A positive integer suitable for runtime limits.
    """
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _message_content_length(message: Any) -> int:
    """Return an approximate text length for a LangChain message."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return len(content)
    return len(str(content)) if content is not None else 0


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

    Three adjustments are needed on top of vanilla
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

    3. The Responses API returns ``AIMessage.content`` as a list of
       structured content blocks (``[{'type': 'text', 'text': '...'},
       ...]``) instead of a plain string. The LangChain agent loop's
       ``OpenAIToolsAgentOutputParser`` expects ``message.content`` to
       be a string and calls ``.strip()`` on it — which raises
       ``'list' object has no attribute 'strip'`` and crashes the
       primary executor (sending the agent down the fallback chain
       to a different model). This subclass overrides every code
       path that produces an AIMessage / AIMessageChunk and flattens
       the text blocks into a plain string before they leave the
       chat model. ``tool_calls`` live on a separate field on the
       message and are unaffected.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        return _move_system_to_instructions(payload)

    # ── Content-flattening overrides ────────────────────────────
    # All four model code paths (sync/async × streaming/non-streaming)
    # need the same fix because the agent executor reaches them via
    # different routes depending on whether AgentExecutor.astream_events
    # or AgentRunner.invoke is the entry point. Each override delegates
    # to the parent and post-processes the result.

    async def _astream(self, *args, **kwargs):
        async for chunk in super()._astream(*args, **kwargs):
            yield _flatten_chat_chunk(chunk)

    def _stream(self, *args, **kwargs):
        for chunk in super()._stream(*args, **kwargs):
            yield _flatten_chat_chunk(chunk)

    def _generate(self, *args, **kwargs):
        result = super()._generate(*args, **kwargs)
        for gen in result.generations:
            _flatten_chat_message(gen.message)
        return result

    async def _agenerate(self, *args, **kwargs):
        result = await super()._agenerate(*args, **kwargs)
        for gen in result.generations:
            _flatten_chat_message(gen.message)
        return result


def _flatten_chat_chunk(chunk):
    """Flatten the content of an AIMessageChunk in-place.

    Returns the same chunk so it can be used inline in a generator.
    """
    if chunk is not None and getattr(chunk, "message", None) is not None:
        _flatten_chat_message(chunk.message)
    return chunk


def _flatten_chat_message(message):
    """Convert AIMessage(Chunk) ``content`` from list-of-blocks to plain string.

    Codex's Responses API and ``ChatOpenAI(use_responses_api=True)``
    return content as a list shaped like::

        [{"type": "text", "text": "...", "index": 0}, ...]

    Some blocks may be reasoning summaries, refusals, or function-call
    metadata — those get stored on ``message.additional_kwargs`` /
    ``message.tool_calls`` by langchain-openai's v0 conversion already,
    so by the time we see the message here only text blocks are
    typically left in ``content``. We concatenate every text block's
    ``text`` field into a single plain string and assign that back to
    ``message.content``.

    The agent loop's ``OpenAIToolsAgentOutputParser`` then sees a
    normal string and ``.strip()`` works.

    Mutates and returns the same message object.
    """
    if message is None or not hasattr(message, "content"):
        return message
    content = message.content
    if not isinstance(content, list):
        return message
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") in ("text", "output_text"):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        elif isinstance(block, str):
            parts.append(block)
    message.content = "".join(parts)
    return message


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
        self.chat_history = []
        self.agent_name = agent_name or "kai"
        self.log = get_logger(self.agent_name)
        self._auto_mode = False
        self._auto_readonly = False
        self._auto_iterations_remaining = 0
        self._is_auto_continuation = False
        self._last_auto_pause_reason: str | None = None
        self._last_auto_state: tuple[str, str | None] = ("unknown", None)
        self._active_llm_chat_history: list[Any] | None = None

        cfg = get_agent_config(agent_name) if agent_name else {}
        self._endpoint_cfg = cfg.get("endpoint")
        # Full chain — first failure walks the list in order
        self._fallback_chain_cfg = cfg.get("fallback_endpoints") or []
        # Backwards-compat alias preserved for any external readers
        self.fallback_endpoint = cfg.get("fallback_endpoint")
        # Persisted so reload_llm() can rebuild the executors
        # without rerunning all of __init__
        self._base_max_iterations = cfg.get("max_iterations", 200)
        self._system_prompt_override = cfg.get("system_prompt")

        # Load persistent memory and register the tool that mutates it.
        # The frozen snapshot is injected into the system prompt here;
        # mid-session writes through the tool update disk + the tool's
        # live state view but never the system prompt (preserves prefix
        # cache and keeps the LLM's notion of "what's in memory" stable
        # for the rest of the turn).
        raw_tools = list(tools)  # copy so we can append the memory tool
        self.memory_store = build_agent_memory_store(self.agent_name)
        memory_block = render_memory_block(self.memory_store)
        raw_tools.append(create_memory_tool(self.memory_store))

        # Load skills and register the three skill tools. Skills are
        # procedural memory — on-demand recipes the agent authored in
        # previous sessions. A minimal catalog is injected into the
        # system prompt so the LLM knows what's on the shelf without
        # paying the token cost of full content; bodies are loaded
        # via the skill_view tool only when needed.
        self.skill_store = build_agent_skill_store(self.agent_name)
        skill_catalog = render_skill_catalog(self.skill_store)
        raw_tools.extend(create_skills_tools(self.skill_store))

        # Compose both memory + skill catalog into the shared prompt
        # "identity" section. Join with a blank line so the headers
        # stay visually distinct.
        self._identity_block = "\n\n".join(b for b in (memory_block, skill_catalog) if b)
        self._raw_tools = list(raw_tools)
        self.tools = [self._wrap_tool(tool) for tool in self._raw_tools]

        self._rebuild_executors()

        log_agent_event(
            self.agent_name,
            "init",
            {
                "endpoint": self._endpoint_cfg.get("base_url") if self._endpoint_cfg else None,
                "model": self._endpoint_cfg.get("model") if self._endpoint_cfg else None,
                "max_iterations": self._current_max_iterations(),
                "fallback_chain": [
                    (f.get("base_url"), f.get("model"))
                    for f in self._fallback_chain_cfg
                ],
                "tools": [t.name for t in self.tools],
            },
        )

    def _build_system_prompt(self) -> str:
        prompt = build_main_system_prompt(
            self._system_prompt_override,
            memory_block=self._identity_block,
        )
        if self._auto_mode:
            prompt = f"{prompt}\n\n{build_auto_suffix(self._auto_iterations_remaining)}"
        return prompt

    def _current_max_iterations(self) -> int:
        if self._auto_mode:
            return max(1, int(self._auto_iterations_remaining or 1))
        return max(1, int(self._base_max_iterations or 1))

    def _endpoint_log_payload(self) -> dict:
        """Return the active model selection in log-friendly form."""
        endpoint_cfg = self._endpoint_cfg or {}
        return {
            "provider": endpoint_cfg.get("provider"),
            "base_url": endpoint_cfg.get("base_url"),
            "model": endpoint_cfg.get("model"),
            "reasoning_effort": endpoint_cfg.get("reasoning_effort"),
            "fallback_chain": [
                {
                    "provider": fallback.get("provider"),
                    "base_url": fallback.get("base_url"),
                    "model": fallback.get("model"),
                }
                for fallback in self._fallback_chain_cfg
            ],
        }

    def _chat_history_for_llm(
        self,
        *,
        exclude_trailing_user_input: bool = False,
    ) -> list[Any]:
        """Return a bounded chat history for one LLM invocation.

        The session still persists complete chat history on disk, but sending
        megabytes of prior turns to every request makes short prompts slow and
        expensive. This keeps the most recent messages within configurable
        count and character limits.

        Args:
            exclude_trailing_user_input: When True, drop the most recent
                ``HumanMessage`` from the rendered history. The
                ``AgentExecutor`` prompt template re-injects ``{input}`` on
                top of ``chat_history``; if the most recent stored entry is
                that same user input the model sees it twice back-to-back
                (real bug surfaced 2026-05-06 in scheduled-job BIO turns).
                Caller passes True from the input-handling path where the
                user message was just appended for persistence.

        Returns:
            Recent chat messages ordered oldest to newest.
        """
        max_messages = _env_int(
            "AGENT_LLM_HISTORY_MAX_MESSAGES",
            DEFAULT_LLM_HISTORY_MAX_MESSAGES,
        )
        max_chars = _env_int(
            "AGENT_LLM_HISTORY_MAX_CHARS",
            DEFAULT_LLM_HISTORY_MAX_CHARS,
        )
        source_history: list[Any] = list(self.chat_history)
        if (
            exclude_trailing_user_input
            and source_history
            and isinstance(source_history[-1], HumanMessage)
        ):
            source_history = source_history[:-1]
        selected: list[Any] = []
        total_chars = 0
        for message in reversed(source_history):
            message_chars = _message_content_length(message)
            if selected and total_chars + message_chars > max_chars:
                break
            selected.append(message)
            total_chars += message_chars
            if len(selected) >= max_messages:
                break
        selected.reverse()
        omitted = max(0, len(self.chat_history) - len(selected))
        if omitted:
            self.log.info(
                "LLM_HISTORY_TRUNCATED agent=%s kept=%d omitted=%d chars=%d",
                self.agent_name,
                len(selected),
                omitted,
                total_chars,
            )
        return selected

    def _rebuild_executors(self) -> None:
        """Rebuild the prompt plus the primary/fallback executors."""

        self._prompt = create_prompt(self._build_system_prompt())
        prompt = self._prompt
        max_iterations = self._current_max_iterations()

        self.llm = create_llm(self._endpoint_cfg)
        primary_agent = create_tool_calling_agent(self.llm, self.tools, prompt)
        self.executor = AgentExecutor(
            agent=primary_agent,
            tools=self.tools,
            verbose=False,
            max_iterations=max_iterations,
            handle_parsing_errors=True,
        )

        self.fallback_executors = []
        for fb_cfg in self._fallback_chain_cfg:
            try:
                fb_llm = create_llm(fb_cfg)
                fb_agent = create_tool_calling_agent(fb_llm, self.tools, prompt)
                self.fallback_executors.append(
                    AgentExecutor(
                        agent=fb_agent,
                        tools=self.tools,
                        verbose=False,
                        max_iterations=max_iterations,
                        handle_parsing_errors=True,
                    )
                )
            except Exception as exc:
                self.log.warning(
                    "fallback executor build failed for %s endpoint=%s: %s",
                    self.agent_name,
                    fb_cfg.get("base_url"),
                    exc,
                )
        self.fallback_executor = self.fallback_executors[0] if self.fallback_executors else None
        endpoint_payload = self._endpoint_log_payload()
        self.log.info(
            "MODEL_SELECTED agent=%s provider=%s model=%s fallback_count=%d",
            self.agent_name,
            endpoint_payload.get("provider"),
            endpoint_payload.get("model"),
            len(self.fallback_executors),
        )
        log_agent_event(self.agent_name, "model_selected", endpoint_payload)

    def set_auto_mode(self, enabled: bool, max_iterations: int = 40):
        """Enable/disable auto mode. Rebuilds prompt + executor."""

        self._auto_mode = bool(enabled)
        self._auto_iterations_remaining = max(1, int(max_iterations)) if enabled else 0
        self._rebuild_for_auto()

    def _rebuild_for_auto(self):
        """Rebuild the system prompt and executor for auto mode."""

        self._rebuild_executors()

    def _check_tool_allowed(self, tool_name: str) -> None:
        policy = get_tool_policy(tool_name)
        logger = getattr(self, "log", get_logger(getattr(self, "agent_name", "kai")))
        allowed = True
        reason: str | None = None
        # Phase 4/5 cutover (epic #10028): when KAI_TRUSTED_AUTONOMOUS=1 is
        # set in the runtime env, bypass the auto-mode approval gate for
        # non-readonly tools. Required for autonomous SDLC fires where the
        # agent is the workforce principal — the operator's approval is the
        # ticket assignment + the SOC2 review chain downstream, not a
        # per-tool prompt that no human is there to grant.
        trusted_autonomous = (
            os.environ.get("KAI_TRUSTED_AUTONOMOUS", "").strip().lower()
            in ("1", "true", "yes", "on")
        )
        if self._auto_mode:
            if self._auto_readonly and not policy.read_only:
                allowed = False
                reason = f"auto readonly blocks non-read-only tool: {tool_name}"
            elif policy.requires_approval_in_auto and not trusted_autonomous:
                allowed = False
                reason = f"requires approval for {tool_name}"
        logger.info(
            "TOOL_POLICY tool=%s allowed=%s trusted_autonomous=%s",
            tool_name,
            allowed,
            trusted_autonomous,
        )
        if not allowed and reason is not None:
            raise RuntimeError(reason)

    def _wrap_tool(self, tool: StructuredTool) -> StructuredTool:
        """Wrap one tool so auto-mode policy checks run before execution."""

        original_sync = getattr(tool, "func", None)
        original_async = getattr(tool, "coroutine", None)

        def _sync_wrapper(*args, **kwargs):
            self._check_tool_allowed(tool.name)
            if original_sync is None:
                raise RuntimeError(f"tool {tool.name} has no sync implementation")
            return original_sync(*args, **kwargs)

        async def _async_wrapper(*args, **kwargs):
            self._check_tool_allowed(tool.name)
            if original_async is not None:
                return await original_async(*args, **kwargs)
            if original_sync is None:
                raise RuntimeError(f"tool {tool.name} has no implementation")
            return original_sync(*args, **kwargs)

        return StructuredTool.from_function(
            func=_sync_wrapper if original_sync is not None else None,
            coroutine=_async_wrapper if original_async is not None else None,
            name=tool.name,
            description=tool.description,
            return_direct=tool.return_direct,
            args_schema=tool.args_schema,
            infer_schema=False,
            response_format=tool.response_format,
            verbose=tool.verbose,
            callbacks=tool.callbacks,
            callback_manager=tool.callback_manager,
            tags=tool.tags,
            metadata=tool.metadata,
            handle_tool_error=tool.handle_tool_error,
            handle_validation_error=tool.handle_validation_error,
        )

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
        self._endpoint_cfg = cfg.get("endpoint")
        self._fallback_chain_cfg = cfg.get("fallback_endpoints") or []
        self._base_max_iterations = cfg.get("max_iterations", self._base_max_iterations)
        self.fallback_endpoint = cfg.get("fallback_endpoint")
        self._rebuild_executors()

        log_agent_event(
            self.agent_name,
            "reload_llm",
            {
                "endpoint": self._endpoint_cfg.get("base_url") if self._endpoint_cfg else None,
                "model": self._endpoint_cfg.get("model") if self._endpoint_cfg else None,
                "fallback_chain": [
                    (f.get("base_url"), f.get("model"))
                    for f in self._fallback_chain_cfg
                ],
            },
        )

        return {
            "endpoint": self._endpoint_cfg.get("base_url") if self._endpoint_cfg else None,
            "model": self._endpoint_cfg.get("model") if self._endpoint_cfg else None,
            "provider": self._endpoint_cfg.get("provider") if self._endpoint_cfg else None,
            "reasoning_effort": (
                self._endpoint_cfg.get("reasoning_effort")
                if self._endpoint_cfg
                else None
            ),
            "fallback_count": len(self.fallback_executors),
        }

    @contextmanager
    def override_max_iterations(self, max_iterations: int | None):
        """Temporarily override the iteration budget for one run."""
        if max_iterations is None:
            yield
            return

        executors = [self.executor, *self.fallback_executors]
        originals = [getattr(executor, "max_iterations", None) for executor in executors]
        for executor in executors:
            if hasattr(executor, "max_iterations"):
                executor.max_iterations = max_iterations
        try:
            yield
        finally:
            for executor, original in zip(executors, originals):
                if original is not None:
                    executor.max_iterations = original

    async def run(
        self,
        user_input: str,
        *,
        pre_injected_input: bool = False,
    ) -> AsyncIterator[dict]:
        """Stream agent events. Falls back to secondary endpoint on error."""
        self._last_auto_pause_reason = None
        self._last_auto_state = ("unknown", None)
        self.log.info(
            "RUN auto=%s iter_remaining=%d provider=%s model=%s",
            bool(getattr(self, "_auto_mode", False)),
            int(getattr(self, "_auto_iterations_remaining", 0) or 0),
            (getattr(self, "_endpoint_cfg", None) or {}).get("provider"),
            (getattr(self, "_endpoint_cfg", None) or {}).get("model"),
        )

        visible_input = not getattr(self, "_is_auto_continuation", False)
        log_input = user_input if visible_input else "[AUTO_CONTINUATION]"
        if visible_input and not pre_injected_input:
            self.chat_history.append(HumanMessage(content=user_input))
        elif not visible_input:
            self.log.info("AUTO_HIDDEN_TURN")
        self.log.info("USER_INPUT agent=%s input=%s", self.agent_name, log_input[:200])
        # Render the LLM-bound history. The LangChain ``AgentExecutor`` prompt
        # template re-injects ``{input}`` as a fresh HumanMessage on top of
        # ``chat_history``, so we must NOT include the just-appended user
        # message in what we hand to the executor — otherwise the model sees
        # the same user turn twice back-to-back. We persist the message in
        # ``self.chat_history`` (so the next turn's history has it) but
        # render the LLM history from everything BEFORE the current append.
        # Bug surfaced 2026-05-06 in scheduled-job BIO turns where Dan saw
        # two identical user messages literally back-to-back before the
        # AI's reply.
        self._active_llm_chat_history = self._chat_history_for_llm(
            exclude_trailing_user_input=visible_input,
        )

        # Log the full prompt at DEBUG level
        log_llm_request(
            self.agent_name,
            self._active_llm_chat_history,
            input=log_input,
            history_total=len(self.chat_history),
            history_sent=len(self._active_llm_chat_history),
        )

        accumulated = ""
        final_text = ""
        primary_failed = False
        emitted_final = False
        fallback_executors = list(
            getattr(
                self,
                "fallback_executors",
                (
                    []
                    if getattr(self, "fallback_executor", None) is None
                    else [self.fallback_executor]
                ),
            )
        )

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
                self.log.warning(
                    "EMPTY_RESPONSE agent=%s endpoint=primary",
                    self.agent_name,
                )
                yield {"type": "error", "data": "Primary endpoint returned an empty response."}

        except RuntimeError as e:
            if getattr(self, "_auto_mode", False) and (
                "requires approval for " in str(e)
                or "auto readonly blocks non-read-only tool:" in str(e)
            ):
                self._last_auto_pause_reason = str(e)
                self._last_auto_state = ("pause", self._last_auto_pause_reason)
                return
            primary_failed = True
            self.log.error("PRIMARY_FAILED agent=%s error=%s", self.agent_name, str(e))
            yield {"type": "error", "data": f"Primary endpoint failed: {e}"}
        except Exception as e:
            primary_failed = True
            self.log.error("PRIMARY_FAILED agent=%s error=%s", self.agent_name, str(e))
            yield {"type": "error", "data": f"Primary endpoint failed: {e}"}

        # Walk the fallback chain. Each entry is tried in order until
        # one returns a non-empty, non-error result.
        attempt = 0
        for fb_executor in fallback_executors:
            if not primary_failed or self._last_auto_pause_reason is not None:
                break
            attempt += 1
            label = f"fallback_{attempt}"
            log_agent_event(self.agent_name, label)
            yield {
                "type": "status",
                "data": f"Falling back to endpoint #{attempt} of {len(fallback_executors)}...",
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
                    self.log.warning(
                        "EMPTY_RESPONSE agent=%s endpoint=%s",
                        self.agent_name,
                        label,
                    )
                    yield {
                        "type": "error",
                        "data": f"Endpoint #{attempt} returned an empty response.",
                    }
            except RuntimeError as e:
                if getattr(self, "_auto_mode", False) and (
                    "requires approval for " in str(e)
                    or "auto readonly blocks non-read-only tool:" in str(e)
                ):
                    self._last_auto_pause_reason = str(e)
                    self._last_auto_state = ("pause", self._last_auto_pause_reason)
                    return
                primary_failed = True
                self.log.error(
                    "FALLBACK_FAILED agent=%s attempt=%d error=%s",
                    self.agent_name,
                    attempt,
                    str(e),
                )
                yield {"type": "error", "data": f"Endpoint #{attempt} failed: {e}"}
                final_text = f"Error: {e}"
            except Exception as e:
                primary_failed = True
                self.log.error(
                    "FALLBACK_FAILED agent=%s attempt=%d error=%s",
                    self.agent_name,
                    attempt,
                    str(e),
                )
                yield {"type": "error", "data": f"Endpoint #{attempt} failed: {e}"}
                final_text = f"Error: {e}"

        if self._last_auto_pause_reason is not None:
            return

        response_text = ensure_non_empty_response(final_text or accumulated)
        if response_text == EMPTY_RESPONSE_ERROR:
            self.log.warning("EMPTY_RESPONSE agent=%s endpoint=final", self.agent_name)
        if not emitted_final or not (final_text or accumulated).strip():
            yield {"type": "final", "data": response_text}
        self.chat_history.append(AIMessage(content=response_text))
        if getattr(self, "_auto_mode", False):
            self._last_auto_state = parse_auto_state(response_text)

        # Log the full response at DEBUG
        log_llm_response(self.agent_name, response_text)
        self.log.info(
            "AGENT_RESPONSE agent=%s length=%d text=%s",
            self.agent_name,
            len(response_text),
            response_text[:500],
        )

        if self.bus and not getattr(self, "_is_auto_continuation", False):
            try:
                await self.bus.publish(
                    f"agent.{self.bus.agent_name}.response",
                    {"response": response_text, "input": user_input},
                )
            except Exception:
                pass

    def consume_auto_pause_reason(self) -> str | None:
        """Return and clear the last runtime pause reason."""

        reason = self._last_auto_pause_reason
        self._last_auto_pause_reason = None
        return reason

    def get_last_auto_state(self) -> tuple[str, str | None]:
        """Return the parsed AUTO_STATE from the last completed turn."""

        return self._last_auto_state

    async def _stream_executor(self, executor, user_input):
        """Stream events from an AgentExecutor."""
        chat_history = self._active_llm_chat_history or self.chat_history
        async for event in executor.astream_events(
            {"input": user_input, "chat_history": chat_history},
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
