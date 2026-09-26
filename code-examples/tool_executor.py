"""
tool_executor.py
================

Reference implementation of the Action Executor (Layer 2) and its bridge to the
Tools & Integrations layer (Layer 3).

Pipeline for every tool call:

    select -> permission check -> argument validation -> execute -> sanitize -> record

Tool sources supported:
  * Local Python functions (decorated with @tool)
  * MCP servers (discovered at start-up via `tools/list`, invoked via `tools/call`)
  * Sandboxed code execution (separate service; never in-process)

Security properties enforced here, not in the agent prompt:
  * Per-agent allow-lists (a researcher cannot call `send_email`).
  * Risk tiers: `write` and `irreversible` tools require human approval.
  * Argument validation against JSON Schema before anything is executed.
  * Timeouts, retries with back-off and a circuit breaker per tool.
  * Tool output is wrapped and length-limited; it is data, never instructions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

import httpx
import jsonschema

log = logging.getLogger("tool_executor")

RiskTier = Literal["read", "write", "irreversible"]


# --------------------------------------------------------------------------- #
# Tool registry
# --------------------------------------------------------------------------- #
@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]              # JSON Schema
    risk: RiskTier
    allowed_agents: set[str]
    handler: Callable[..., Awaitable[Any]]
    timeout_s: float = 20.0
    max_retries: int = 2
    source: str = "local"                   # "local" | "mcp:<server>"

    def openai_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        return self._tools[name]

    def for_agent(self, agent: str) -> list[ToolSpec]:
        return [t for t in self._tools.values() if agent in t.allowed_agents]


def tool(*, risk: RiskTier, agents: set[str], parameters: dict[str, Any], timeout_s: float = 20.0):
    """Decorator that turns an async function into a registered ToolSpec."""
    def wrap(fn):
        fn.__tool_spec__ = ToolSpec(name=fn.__name__, description=fn.__doc__ or "",
                                    parameters=parameters, risk=risk, allowed_agents=agents,
                                    handler=fn, timeout_s=timeout_s)
        return fn
    return wrap


# --------------------------------------------------------------------------- #
# MCP client (HTTP transport). Discovers tools and exposes them as ToolSpecs.
# --------------------------------------------------------------------------- #
class MCPServer:
    def __init__(self, name: str, url: str, token: str, default_risk: RiskTier = "read",
                 allowed_agents: set[str] | None = None):
        self.name, self.url, self.token = name, url, token
        self.default_risk = default_risk
        self.allowed_agents = allowed_agents or {"researcher", "analyst", "coder"}
        self.http = httpx.AsyncClient(base_url=url, timeout=30,
                                      headers={"Authorization": f"Bearer {token}"})

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        r = await self.http.post("/", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"MCP {self.name} {method}: {body['error']}")
        return body["result"]

    async def discover(self, registry: ToolRegistry, risk_overrides: dict[str, RiskTier] | None = None):
        """tools/list -> register every tool with a namespaced name `<server>__<tool>`."""
        result = await self._rpc("tools/list", {})
        for t in result["tools"]:
            name = f"{self.name}__{t['name']}"
            risk = (risk_overrides or {}).get(t["name"], self.default_risk)
            # bind loop variable
            async def handler(_tool=t["name"], **kwargs):
                res = await self._rpc("tools/call", {"name": _tool, "arguments": kwargs})
                return res.get("content", res)
            registry.register(ToolSpec(name=name, description=t.get("description", ""),
                                       parameters=t.get("inputSchema", {"type": "object"}),
                                       risk=risk, allowed_agents=self.allowed_agents,
                                       handler=handler, source=f"mcp:{self.name}"))
        log.info("MCP %s: registered %d tools", self.name, len(result["tools"]))


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
@dataclass
class Breaker:
    failures: int = 0
    opened_at: float | None = None
    threshold: int = 5
    cooldown_s: float = 60.0

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.time() - self.opened_at > self.cooldown_s:
            self.opened_at, self.failures = None, 0
            return True
        return False

    def record(self, ok: bool) -> None:
        if ok:
            self.failures = 0
        else:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.time()


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #
class ApprovalRequired(Exception):
    def __init__(self, tool: str, arguments: dict[str, Any]):
        super().__init__(f"human approval required for {tool}")
        self.tool, self.arguments = tool, arguments


class ToolExecutor:
    MAX_RESULT_CHARS = 12_000

    def __init__(self, registry: ToolRegistry, approvals, audit, policy=None):
        """
        approvals: object with `async request(tool, arguments, principal) -> bool`
        audit:     object with `async log(event: dict)`
        policy:    optional ABAC engine with `allows(principal, tool_spec, arguments) -> bool`
        """
        self.registry, self.approvals, self.audit, self.policy = registry, approvals, audit, policy
        self._breakers: dict[str, Breaker] = {}

    def schemas_for(self, agent: str) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self.registry.for_agent(agent)]

    async def execute(self, *, agent: str, name: str, arguments: dict[str, Any],
                      principal: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        event = {"agent": agent, "tool": name, "principal": principal.get("sub"),
                 "arguments": arguments, "ts": started}
        try:
            spec = self._authorize(agent, name, arguments, principal)
            jsonschema.validate(arguments, spec.parameters)
            if spec.risk in ("write", "irreversible"):
                approved = await self.approvals.request(name, arguments, principal)
                if not approved:
                    raise PermissionError(f"{name} rejected by reviewer")
            raw = await self._call_with_resilience(spec, arguments)
            result = self._sanitize(raw)
            event.update(status="ok", duration_ms=int((time.time() - started) * 1000))
            return {"ok": True, "data": result}
        except (PermissionError, KeyError, jsonschema.ValidationError) as e:
            event.update(status="denied", error=str(e))
            return {"ok": False, "error": str(e)}          # agent sees a clean error, can re-plan
        except Exception as e:                              # noqa: BLE001
            event.update(status="error", error=repr(e))
            return {"ok": False, "error": f"tool failure: {type(e).__name__}"}
        finally:
            await self.audit.log(event)

    # ---- steps -------------------------------------------------------------- #
    def _authorize(self, agent: str, name: str, arguments: dict, principal: dict) -> ToolSpec:
        spec = self.registry.get(name)                      # KeyError -> unknown tool
        if agent not in spec.allowed_agents:
            raise PermissionError(f"agent '{agent}' may not call {name}")
        if self.policy and not self.policy.allows(principal, spec, arguments):
            raise PermissionError(f"policy denies {name} for {principal.get('sub')}")
        return spec

    async def _call_with_resilience(self, spec: ToolSpec, arguments: dict) -> Any:
        breaker = self._breakers.setdefault(spec.name, Breaker())
        if not breaker.allow():
            raise RuntimeError(f"{spec.name} circuit open")
        delay = 0.5
        for attempt in range(spec.max_retries + 1):
            try:
                result = await asyncio.wait_for(spec.handler(**arguments), timeout=spec.timeout_s)
                breaker.record(True)
                return result
            except (asyncio.TimeoutError, httpx.TransportError) as e:
                breaker.record(False)
                if attempt == spec.max_retries or spec.risk != "read":   # never retry writes blindly
                    raise
                log.warning("retrying %s after %s", spec.name, e)
                await asyncio.sleep(delay)
                delay *= 2

    def _sanitize(self, raw: Any) -> Any:
        """Bound size and strip control characters. Content is still untrusted."""
        text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
        text = "".join(ch for ch in text if ch == "\n" or ch >= " ")
        if len(text) > self.MAX_RESULT_CHARS:
            text = text[: self.MAX_RESULT_CHARS] + "\n...[truncated]"
        return text


# --------------------------------------------------------------------------- #
# Example local tools
# --------------------------------------------------------------------------- #
@tool(risk="read", agents={"researcher"}, parameters={
    "type": "object", "properties": {"query": {"type": "string", "maxLength": 300},
                                     "top_k": {"type": "integer", "minimum": 1, "maximum": 10}},
    "required": ["query"], "additionalProperties": False})
async def web_search(query: str, top_k: int = 5) -> list[dict[str, str]]:
    """Search the web and return title, url and snippet for the top results."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://search.internal/api", params={"q": query, "n": top_k})
        r.raise_for_status()
        return r.json()["results"]


@tool(risk="read", agents={"coder", "analyst"}, timeout_s=60, parameters={
    "type": "object", "properties": {"code": {"type": "string", "maxLength": 20000},
                                     "language": {"type": "string", "enum": ["python"]}},
    "required": ["code"], "additionalProperties": False})
async def run_code(code: str, language: str = "python") -> dict[str, Any]:
    """Execute code in an isolated sandbox (no network, 512MB RAM, 30s CPU) and return stdout/stderr."""
    async with httpx.AsyncClient(timeout=70) as c:
        r = await c.post("http://sandbox.internal/execute",
                         json={"language": language, "code": code,
                               "limits": {"cpu_s": 30, "memory_mb": 512, "network": False}})
        r.raise_for_status()
        return r.json()


@tool(risk="write", agents={"analyst"}, parameters={
    "type": "object", "properties": {"to": {"type": "string", "format": "email"},
                                     "subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["to", "subject", "body"], "additionalProperties": False})
async def send_email(to: str, subject: str, body: str) -> dict[str, str]:
    """Send an email on behalf of the user. Requires human approval."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post("http://mail.internal/send", json={"to": to, "subject": subject, "body": body})
        r.raise_for_status()
        return {"message_id": r.json()["id"]}


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for fn in (web_search, run_code, send_email):
        reg.register(fn.__tool_spec__)
    return reg
