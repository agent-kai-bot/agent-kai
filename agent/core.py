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
    """Create a ChatOpenAI instance from an endpoint config dict."""
    if endpoint_cfg is None:
        first = next(iter(ENDPOINTS), None)
        endpoint_cfg = get_endpoint(first) if first else {}
    return ChatOpenAI(
        base_url=endpoint_cfg["base_url"],
        api_key=endpoint_cfg.get("api_key", "not-needed"),
        model=endpoint_cfg["model"],
        temperature=endpoint_cfg.get("temperature", 0.6),
        top_p=endpoint_cfg.get("top_p", 0.95),
        max_tokens=endpoint_cfg.get("max_tokens", 4096),
        streaming=True,
    )


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
        self.agent_name = agent_name or "nano"
        self.log = get_logger(self.agent_name)

        cfg = get_agent_config(agent_name) if agent_name else {}
        ep = cfg.get("endpoint")
        self.fallback_endpoint = cfg.get("fallback_endpoint")
        max_iterations = cfg.get("max_iterations", 200)
        system_prompt = cfg.get("system_prompt")

        self.llm = create_llm(ep)
        self.fallback_llm = create_llm(self.fallback_endpoint) if self.fallback_endpoint else None

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

        prompt = create_prompt(
            build_main_system_prompt(system_prompt, memory_block=identity_block)
        )
        agent = create_tool_calling_agent(self.llm, self.tools, prompt)
        self.executor = AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=False,
            max_iterations=max_iterations,
            handle_parsing_errors=True,
        )

        self.fallback_executor = None
        if self.fallback_llm:
            fallback_agent = create_tool_calling_agent(self.fallback_llm, self.tools, prompt)
            self.fallback_executor = AgentExecutor(
                agent=fallback_agent,
                tools=self.tools,
                verbose=False,
                max_iterations=max_iterations,
                handle_parsing_errors=True,
            )

        log_agent_event(self.agent_name, "init", {
            "endpoint": ep.get("base_url") if ep else None,
            "model": ep.get("model") if ep else None,
            "max_iterations": max_iterations,
            "tools": [t.name for t in self.tools],
        })

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

        if primary_failed and self.fallback_executor:
            log_agent_event(self.agent_name, "fallback")
            yield {"type": "status", "data": "Falling back to secondary endpoint..."}
            accumulated = ""
            try:
                async for event in self._stream_executor(self.fallback_executor, user_input):
                    yield event
                    if event["type"] == "token":
                        accumulated += event["data"]
                    elif event["type"] == "final":
                        final_text = event["data"]
                        emitted_final = True
            except Exception as e:
                self.log.error("FALLBACK_FAILED agent=%s error=%s", self.agent_name, str(e))
                yield {"type": "error", "data": f"Fallback also failed: {e}"}
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
