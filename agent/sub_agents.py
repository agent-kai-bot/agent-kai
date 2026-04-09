"""Sub-agent spawner and manager."""

import asyncio

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from agent.core import build_agent_memory_store, create_llm, render_memory_block
from agent.memory_tool import create_memory_tool
from agent.prompts import build_sub_agent_system_prompt
from agent.runtime_utils import EMPTY_RESPONSE_ERROR, ensure_non_empty_response
from agent.tools import create_sub_agent_tools
from agent_logger import get_logger, log_agent_event, log_llm_request, log_llm_response
from config import get_agent_config

FINAL_RESPONSE_RETRY_PROMPT = (
    "\n\nProvide the final written answer for the caller now. "
    "Do not leave the response blank. "
    "If you already gathered enough data, summarize it directly without calling more tools."
)


class SubAgent:
    """A background agent that listens on NATS for tasks."""

    def __init__(self, name, bus, system_prompt=None):
        self.name = name
        self.bus = bus

        # Load config for this agent name (falls back to defaults if not in config)
        cfg = get_agent_config(name)
        endpoint_cfg = cfg.get("endpoint")
        fallback_cfg = cfg.get("fallback_endpoint")
        max_iterations = cfg.get("max_iterations", 10)
        self.workspace = cfg.get("workspace", "")

        # Pass the workspace path into the tool factory so the
        # docker_sandbox tool can bind-mount it as /work. Without this
        # the sub-agent couldn't share files with its sandboxed runs.
        tools = create_sub_agent_tools(bus, workspace_host_path=self.workspace or None)

        # Per-agent persistent memory — each sub-agent curates its own
        # MEMORY.md under its workspace but shares USER.md with every
        # other agent in the process. Loaded here so the frozen
        # snapshot is captured before the system prompt is assembled.
        self.memory_store = build_agent_memory_store(name)
        memory_block = render_memory_block(self.memory_store)
        tools = list(tools) + [create_memory_tool(self.memory_store)]

        llm = create_llm(endpoint_cfg)

        role_prompt = system_prompt or cfg.get("system_prompt")
        prompt_text = build_sub_agent_system_prompt(
            agent_name=name,
            role_prompt=role_prompt,
            workspace=self.workspace,
            memory_block=memory_block,
        )
        # Escape curly braces so LangChain doesn't treat them as template vars
        prompt_text = prompt_text.replace("{", "{{").replace("}", "}}")
        prompt = ChatPromptTemplate.from_messages([
            ("system", prompt_text),
            MessagesPlaceholder("chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ])

        agent = create_tool_calling_agent(llm, tools, prompt)
        self.executor = AgentExecutor(
            agent=agent,
            tools=tools,
            verbose=False,
            max_iterations=max_iterations,
            handle_parsing_errors=True,
        )

        # Build fallback executor if configured
        self.fallback_executor = None
        if fallback_cfg:
            fallback_llm = create_llm(fallback_cfg)
            fallback_agent = create_tool_calling_agent(fallback_llm, tools, prompt)
            self.fallback_executor = AgentExecutor(
                agent=fallback_agent,
                tools=tools,
                verbose=False,
                max_iterations=max_iterations,
                handle_parsing_errors=True,
            )

        self.chat_history = []
        self._running = False
        self.log = get_logger(name)

        log_agent_event(name, "sub_agent_init", {
            "endpoint": endpoint_cfg.get("base_url") if endpoint_cfg else None,
            "workspace": self.workspace,
        })

    async def start(self):
        """Subscribe to NATS and start listening for tasks."""
        self._running = True
        await self.bus.subscribe_with_reply(
            f"agent.{self.name}.request",
            self._handle_request,
        )
        await self.bus.publish("system.registry", {
            "agent": self.name,
            "status": "online",
            "type": "sub-agent",
        })
        log_agent_event(self.name, "started")

    async def stop(self):
        self._running = False
        await self.bus.publish("system.registry", {
            "agent": self.name,
            "status": "offline",
        })
        log_agent_event(self.name, "stopped")

    async def _handle_request(self, subject, payload):
        """Process a task. Try primary endpoint, fall back if it fails."""
        task = payload.get("task") or payload.get("message", "")
        if not task:
            return {"error": "no task provided", "from": self.name}

        self.log.info("REQUEST agent=%s from=%s task=%s", self.name, payload.get("from", "?"), task[:200])
        log_llm_request(self.name, self.chat_history, input=task)

        await self.bus.publish(f"agent.{self.name}.status", {
            "state": "thinking", "task": task[:100],
        })

        output = await self._invoke_with_fallback(task, publish_status=True)

        log_llm_response(self.name, output)
        self.log.info("RESPONSE agent=%s length=%d", self.name, len(output))

        await self.bus.publish(f"agent.{self.name}.status", {"state": "idle"})
        await self.bus.publish(f"agent.{self.name}.response", {
            "response": output, "task": task[:100],
        })
        return {"response": output, "from": self.name}

    async def _invoke(self, executor, task):
        """Run an executor and return the output string."""
        try:
            result = await executor.ainvoke({
                "input": task,
                "chat_history": self.chat_history,
            })
            output = ensure_non_empty_response(result.get("output", ""))
            if output.startswith("Error:"):
                self.log.warning("EMPTY_RESPONSE agent=%s executor=%s", self.name, type(executor).__name__)
            return output
        except Exception as e:
            self.log.error("INVOKE_ERROR agent=%s error=%s", self.name, str(e))
            return f"Error: {e}"

    async def _invoke_with_fallback(self, task: str, publish_status: bool = False) -> str:
        """Run the primary executor and fall back on error-like output.

        Args:
            task: Task text to execute.
            publish_status: Whether to emit a NATS status update before fallback.

        Returns:
            The primary or fallback output.
        """
        active_executor = self.executor
        output = await self._invoke(active_executor, task)
        if output.startswith("Error:") and self.fallback_executor:
            self.log.warning("PRIMARY_FAILED agent=%s, trying fallback", self.name)
            log_agent_event(self.name, "fallback")
            if publish_status and self.bus:
                await self.bus.publish(
                    f"agent.{self.name}.status",
                    {"state": "fallback", "task": task[:100]},
                )
            active_executor = self.fallback_executor
            output = await self._invoke(active_executor, task)
        if output == EMPTY_RESPONSE_ERROR:
            retry_output = await self._invoke(active_executor, f"{task}{FINAL_RESPONSE_RETRY_PROMPT}")
            if retry_output != EMPTY_RESPONSE_ERROR:
                output = retry_output
        return output

    async def run_once(self, task: str) -> str:
        """Run a single task directly (without NATS)."""
        return await self._invoke_with_fallback(task)


class SubAgentManager:
    """Tracks and manages spawned sub-agents."""

    def __init__(self, bus):
        self.bus = bus
        self.agents: dict[str, SubAgent] = {}

    async def spawn(self, name: str, system_prompt: str = None,
                    initial_task: str = None) -> str:
        if name in self.agents:
            return f"Agent '{name}' already exists."
        if name == self.bus.agent_name:
            return f"Cannot spawn agent with same name as main agent ('{name}')."

        agent = SubAgent(name, self.bus, system_prompt=system_prompt)
        await agent.start()
        self.agents[name] = agent

        cfg = get_agent_config(name)
        desc = cfg.get("description", "")
        ws = agent.workspace or "none"

        result = f"Sub-agent '{name}' spawned (workspace: {ws})"
        if desc:
            result += f" — {desc}"
        result += f"\nListening on agent.{name}.request"

        if initial_task:
            asyncio.create_task(self._send_initial_task(name, initial_task))
            result += " — initial task queued."

        return result

    async def _send_initial_task(self, name: str, task: str):
        await asyncio.sleep(0.1)
        await self.bus.publish(f"agent.{name}.request", {
            "task": task, "from": self.bus.agent_name,
        })

    async def stop(self, name: str) -> str:
        if name not in self.agents:
            return f"No agent named '{name}'."
        await self.agents[name].stop()
        del self.agents[name]
        return f"Agent '{name}' stopped."

    def list_agents(self) -> list[str]:
        return list(self.agents.keys())

    async def stop_all(self):
        for name in list(self.agents.keys()):
            await self.stop(name)
