"""
agent_orchestrator.py
=====================

Reference implementation of the Agent Orchestrator (Layer 2) using LangGraph.

Responsibilities demonstrated here:
  * Planner / Reasoner node that decomposes a user goal into a task DAG.
  * Dispatch of tasks to specialised sub-agents (Researcher, Analyst, Coder).
  * Bounded ReAct loops with explicit step / token budgets (no runaway agents).
  * Shared state that every node reads from and writes to.
  * Reflection step before final synthesis.
  * Checkpointing so a run can be paused for human approval and resumed.

Dependencies (see requirements.txt in the reference implementation):
  langgraph>=0.2, pydantic>=2, httpx

The LLM gateway, memory manager, tool executor and guardrails are injected as
interfaces so the orchestrator stays independent of any vendor.
"""

from __future__ import annotations

import json
import operator
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol, TypedDict

from langgraph.checkpoint.memory import MemorySaver  # swap for PostgresSaver in prod
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Injected interfaces (implemented in llm_gateway.py, memory_manager.py, ...)
# --------------------------------------------------------------------------- #
class LLMGateway(Protocol):
    async def complete(
        self, *, task_class: str, messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None, max_tokens: int = 2048,
    ) -> dict[str, Any]: ...


class MemoryManager(Protocol):
    async def load_context(self, session_id: str, user_id: str, query: str) -> dict[str, Any]: ...
    async def append_turn(self, session_id: str, role: str, content: str) -> None: ...
    async def remember(self, user_id: str, fact: str, kind: str) -> None: ...


class ToolExecutor(Protocol):
    def schemas_for(self, agent: str) -> list[dict[str, Any]]: ...
    async def execute(self, *, agent: str, name: str, arguments: dict[str, Any],
                      principal: dict[str, Any]) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# Plan schema – the planner is forced to emit this structure as JSON
# --------------------------------------------------------------------------- #
class Task(BaseModel):
    id: str
    agent: Literal["researcher", "analyst", "coder"]
    goal: str
    depends_on: list[str] = Field(default_factory=list)
    acceptance: str = Field(description="How the orchestrator knows the task is done")


class Plan(BaseModel):
    objective: str
    tasks: list[Task]
    final_deliverable: str


# --------------------------------------------------------------------------- #
# Shared graph state
# --------------------------------------------------------------------------- #
class RunState(TypedDict, total=False):
    run_id: str
    session_id: str
    user_id: str
    principal: dict[str, Any]          # identity + roles, used for tool permissions
    request: str
    context: dict[str, Any]            # memory bundle loaded at start
    plan: dict[str, Any]
    results: Annotated[dict[str, Any], operator.or_]   # task_id -> result (merged)
    pending: list[str]                 # task ids not yet executed
    critique: str
    revisions: int
    final: str
    budget: dict[str, int]             # remaining steps / tokens
    awaiting_approval: dict[str, Any] | None


@dataclass
class Budget:
    max_plan_tasks: int = 8
    max_steps_per_task: int = 6
    max_revisions: int = 1
    max_total_tokens: int = 150_000


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
PLANNER_SYSTEM = """You are the planning component of an enterprise agent system.
Decompose the user's objective into the smallest set of independent tasks.
Each task is executed by exactly one specialist:
  - researcher: gathers facts from search, documents and databases
  - analyst: interprets data, compares options, produces judgments
  - coder: writes and runs code in a sandbox for computation or transformation
Respond ONLY with JSON matching this schema:
{schema}
Never produce more than {max_tasks} tasks."""

AGENT_SYSTEM = {
    "researcher": "You are a research specialist. Use tools to gather verifiable facts. "
                  "Cite the source for every claim. Stop when the goal is met.",
    "analyst": "You are an analyst. Interpret provided evidence, weigh trade-offs and "
               "state confidence. Do not invent data; request it if missing.",
    "coder": "You are a coding specialist. Write code, execute it in the sandbox tool, "
             "inspect the output and fix errors. Return results, not just code.",
}


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
class AgentOrchestrator:
    def __init__(self, gateway: LLMGateway, memory: MemoryManager,
                 tools: ToolExecutor, budget: Budget | None = None,
                 checkpointer=None):
        self.gw = gateway
        self.memory = memory
        self.tools = tools
        self.budget = budget or Budget()
        self.graph = self._build_graph(checkpointer or MemorySaver())

    # ---- graph construction ------------------------------------------------ #
    def _build_graph(self, checkpointer):
        g = StateGraph(RunState)
        g.add_node("load_context", self.load_context)
        g.add_node("plan", self.plan)
        g.add_node("execute_task", self.execute_task)
        g.add_node("reflect", self.reflect)
        g.add_node("synthesize", self.synthesize)
        g.add_node("persist", self.persist)

        g.set_entry_point("load_context")
        g.add_edge("load_context", "plan")
        g.add_edge("plan", "execute_task")
        g.add_conditional_edges("execute_task", self._after_task,
                                {"next": "execute_task", "reflect": "reflect"})
        g.add_conditional_edges("reflect", self._after_reflect,
                                {"revise": "plan", "accept": "synthesize"})
        g.add_edge("synthesize", "persist")
        g.add_edge("persist", END)
        # interrupt_before lets a human approve high-risk tool calls (see execute_task)
        return g.compile(checkpointer=checkpointer, interrupt_before=[])

    # ---- nodes ------------------------------------------------------------- #
    async def load_context(self, state: RunState) -> RunState:
        ctx = await self.memory.load_context(state["session_id"], state["user_id"], state["request"])
        return {"context": ctx, "results": {}, "revisions": 0,
                "budget": {"tokens": self.budget.max_total_tokens}}

    async def plan(self, state: RunState) -> RunState:
        msgs = [
            {"role": "system", "content": PLANNER_SYSTEM.format(
                schema=json.dumps(Plan.model_json_schema()), max_tasks=self.budget.max_plan_tasks)},
            {"role": "user", "content": self._planning_input(state)},
        ]
        out = await self.gw.complete(task_class="reasoning", messages=msgs, max_tokens=1500)
        plan = Plan.model_validate_json(out["text"])           # raises on malformed JSON
        order = self._topological_order(plan)
        return {"plan": plan.model_dump(), "pending": order, "results": {}}

    async def execute_task(self, state: RunState) -> RunState:
        task_id = state["pending"][0]
        task = next(t for t in state["plan"]["tasks"] if t["id"] == task_id)
        deps = {d: state["results"].get(d) for d in task["depends_on"]}
        result = await self._react_loop(task, deps, state)
        return {"results": {task_id: result}, "pending": state["pending"][1:]}

    async def reflect(self, state: RunState) -> RunState:
        msgs = [
            {"role": "system", "content": "You are a strict reviewer. Identify gaps, unsupported "
                                          "claims or missing deliverables. Reply 'ACCEPT' or 'REVISE: <reasons>'."},
            {"role": "user", "content": json.dumps({"objective": state["plan"]["objective"],
                                                    "results": state["results"]}, default=str)[:20_000]},
        ]
        out = await self.gw.complete(task_class="reasoning", messages=msgs, max_tokens=600)
        return {"critique": out["text"]}

    async def synthesize(self, state: RunState) -> RunState:
        msgs = [
            {"role": "system", "content": "Write the final deliverable for the user. Use only the "
                                          "evidence in the task results. Keep citations."},
            {"role": "user", "content": json.dumps({
                "deliverable": state["plan"]["final_deliverable"],
                "results": state["results"]}, default=str)[:60_000]},
        ]
        out = await self.gw.complete(task_class="generation", messages=msgs, max_tokens=4000)
        return {"final": out["text"]}

    async def persist(self, state: RunState) -> RunState:
        await self.memory.append_turn(state["session_id"], "user", state["request"])
        await self.memory.append_turn(state["session_id"], "assistant", state["final"])
        await self.memory.remember(state["user_id"],
                                   f"Completed objective: {state['plan']['objective']}", kind="episode")
        return {}

    # ---- edges ------------------------------------------------------------- #
    def _after_task(self, state: RunState) -> str:
        return "next" if state["pending"] else "reflect"

    def _after_reflect(self, state: RunState) -> str:
        needs_revision = state["critique"].strip().upper().startswith("REVISE")
        if needs_revision and state["revisions"] < self.budget.max_revisions:
            state["revisions"] += 1
            return "revise"
        return "accept"

    # ---- sub-agent ReAct loop --------------------------------------------- #
    async def _react_loop(self, task: dict, deps: dict, state: RunState) -> dict[str, Any]:
        agent = task["agent"]
        tool_schemas = self.tools.schemas_for(agent)
        msgs = [
            {"role": "system", "content": AGENT_SYSTEM[agent]},
            {"role": "user", "content": json.dumps({
                "goal": task["goal"], "acceptance": task["acceptance"],
                "inputs_from_dependencies": deps,
                "user_context": state["context"].get("profile", {}),
            }, default=str)},
        ]
        observations: list[dict[str, Any]] = []

        for step in range(self.budget.max_steps_per_task):
            out = await self.gw.complete(task_class=f"agent:{agent}", messages=msgs, tools=tool_schemas)
            state["budget"]["tokens"] -= out.get("usage", {}).get("total_tokens", 0)
            if state["budget"]["tokens"] <= 0:
                return {"status": "budget_exhausted", "observations": observations}

            if not out.get("tool_calls"):                          # model finished
                return {"status": "done", "answer": out["text"], "observations": observations}

            msgs.append({"role": "assistant", "content": out.get("text", ""),
                         "tool_calls": out["tool_calls"]})
            for call in out["tool_calls"]:
                result = await self.tools.execute(agent=agent, name=call["name"],
                                                  arguments=call["arguments"],
                                                  principal=state["principal"])
                observations.append({"tool": call["name"], "args": call["arguments"],
                                     "result": result})
                # Tool output is untrusted data - it is wrapped, never interpreted as instructions.
                msgs.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps({"tool_result": result}, default=str)[:12_000]})

        return {"status": "max_steps", "observations": observations}

    # ---- helpers ------------------------------------------------------------ #
    def _planning_input(self, state: RunState) -> str:
        ctx = state["context"]
        parts = [f"OBJECTIVE:\n{state['request']}"]
        if ctx.get("recent_turns"):
            parts.append("RECENT CONVERSATION:\n" + json.dumps(ctx["recent_turns"][-6:]))
        if ctx.get("relevant_memories"):
            parts.append("RELEVANT MEMORY:\n" + "\n".join(ctx["relevant_memories"][:10]))
        if state.get("critique"):
            parts.append("PREVIOUS ATTEMPT WAS REJECTED BECAUSE:\n" + state["critique"])
        return "\n\n".join(parts)

    @staticmethod
    def _topological_order(plan: Plan) -> list[str]:
        remaining = {t.id: set(t.depends_on) for t in plan.tasks}
        order: list[str] = []
        while remaining:
            ready = [t for t, deps in remaining.items() if not deps - set(order)]
            if not ready:
                raise ValueError("Plan contains a dependency cycle")
            order.extend(sorted(ready))
            for t in ready:
                del remaining[t]
        return order

    # ---- public API ----------------------------------------------------------- #
    async def run(self, *, request: str, session_id: str, user_id: str,
                  principal: dict[str, Any]) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": run_id}}
        final_state = await self.graph.ainvoke(
            {"run_id": run_id, "session_id": session_id, "user_id": user_id,
             "principal": principal, "request": request}, config=config)
        return {"run_id": run_id, "answer": final_state["final"],
                "plan": final_state["plan"], "results": final_state["results"]}
