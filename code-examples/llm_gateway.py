"""
llm_gateway.py
==============

Reference implementation of an LLM Gateway client (Layer 6) with:

  * Task-class based model routing (simple -> small/cheap, reasoning -> large)
  * Provider load balancing and ordered failover
  * Semantic-free exact response cache (Redis) and provider prompt caching
  * Token metering, cost attribution and per-tenant quotas
  * Model version pinning with explicit lifecycle metadata
  * Unified response shape independent of provider

In production this logic usually lives in a dedicated gateway service
(LiteLLM proxy, Azure API Management, Kong AI Gateway, a custom service).
The client below shows the *behaviour* the gateway must provide; it can run
in-process for small deployments or be pointed at a proxy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import redis.asyncio as redis

log = logging.getLogger("llm_gateway")


# --------------------------------------------------------------------------- #
# Model catalogue with lifecycle metadata (versioning)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelSpec:
    id: str                     # gateway-side stable alias, e.g. "reasoning-large"
    provider: str               # openai | anthropic | google | azure | vllm
    model: str                  # provider model string, pinned to a dated version
    input_cost_per_1k: float    # USD
    output_cost_per_1k: float
    context_window: int
    supports_tools: bool = True
    supports_prompt_cache: bool = False
    status: str = "active"      # active | deprecated | retired
    weight: int = 1             # for weighted load balancing


CATALOG: dict[str, list[ModelSpec]] = {
    # task_class -> ordered failover list (first entries preferred)
    "reasoning": [
        ModelSpec("reasoning-a", "anthropic", "claude-sonnet-4-5-20250929", 3.0, 15.0, 200_000, supports_prompt_cache=True),
        ModelSpec("reasoning-b", "openai", "gpt-4.1-2025-04-14", 2.0, 8.0, 1_000_000),
        ModelSpec("reasoning-c", "google", "gemini-2.5-pro", 1.25, 10.0, 1_000_000),
    ],
    "generation": [
        ModelSpec("gen-a", "openai", "gpt-4.1-2025-04-14", 2.0, 8.0, 1_000_000, weight=2),
        ModelSpec("gen-b", "anthropic", "claude-sonnet-4-5-20250929", 3.0, 15.0, 200_000, supports_prompt_cache=True),
    ],
    "simple": [
        ModelSpec("small-a", "vllm", "meta-llama/Llama-3.1-8B-Instruct", 0.05, 0.08, 128_000),
        ModelSpec("small-b", "openai", "gpt-4.1-mini-2025-04-14", 0.4, 1.6, 1_000_000),
    ],
    "agent:researcher": [ModelSpec("r-a", "openai", "gpt-4.1-2025-04-14", 2.0, 8.0, 1_000_000)],
    "agent:analyst": [ModelSpec("an-a", "anthropic", "claude-sonnet-4-5-20250929", 3.0, 15.0, 200_000, supports_prompt_cache=True)],
    "agent:coder": [ModelSpec("c-a", "anthropic", "claude-sonnet-4-5-20250929", 3.0, 15.0, 200_000, supports_prompt_cache=True)],
}

PROVIDER_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "azure": "https://{resource}.openai.azure.com/openai/deployments/{model}/chat/completions?api-version=2024-10-21",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "google": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    "vllm": "http://vllm.internal:8000/v1/chat/completions",   # OpenAI-compatible
}


# --------------------------------------------------------------------------- #
# Gateway
# --------------------------------------------------------------------------- #
@dataclass
class GatewayResponse:
    text: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]
    model: str
    provider: str
    cost_usd: float
    cached: bool = False
    latency_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__


class QuotaExceeded(Exception):
    pass


class LLMGateway:
    def __init__(self, secrets: dict[str, str], cache: redis.Redis, meter,
                 *, cache_ttl_s: int = 3600, request_timeout_s: float = 60.0):
        """
        secrets: provider -> API key (loaded from Vault / Secrets Manager, never from code)
        meter:   object with `async record(tenant, model, usage, cost)` and `async remaining(tenant) -> float`
        """
        self.secrets, self.cache, self.meter = secrets, cache, meter
        self.cache_ttl_s = cache_ttl_s
        self.http = httpx.AsyncClient(timeout=request_timeout_s)
        self._unhealthy_until: dict[str, float] = {}     # provider:model -> timestamp

    # ---- public --------------------------------------------------------------- #
    async def complete(self, *, task_class: str, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None = None, max_tokens: int = 2048,
                       temperature: float = 0.2, tenant: str = "default",
                       cacheable: bool = True) -> dict[str, Any]:
        if await self.meter.remaining(tenant) <= 0:
            raise QuotaExceeded(f"tenant {tenant} exhausted its budget")

        candidates = self._route(task_class, messages, tools)
        cache_key = self._cache_key(candidates[0], messages, tools, max_tokens, temperature)
        if cacheable and temperature == 0 and (hit := await self.cache.get(cache_key)):
            r = GatewayResponse(**json.loads(hit)); r.cached = True
            return r.as_dict()

        last_err: Exception | None = None
        for spec in candidates:                           # ordered failover
            if self._is_unhealthy(spec):
                continue
            try:
                started = time.time()
                resp = await self._call(spec, messages, tools, max_tokens, temperature)
                resp.latency_ms = int((time.time() - started) * 1000)
                await self.meter.record(tenant, spec.model, resp.usage, resp.cost_usd)
                if cacheable and temperature == 0:
                    await self.cache.set(cache_key, json.dumps(resp.as_dict()), ex=self.cache_ttl_s)
                return resp.as_dict()
            except (httpx.HTTPStatusError, httpx.TransportError, asyncio.TimeoutError) as e:
                last_err = e
                self._mark_unhealthy(spec, e)
                log.warning("provider %s/%s failed: %s - failing over", spec.provider, spec.model, e)
        raise RuntimeError(f"all providers failed for {task_class}: {last_err}")

    # ---- routing ---------------------------------------------------------------- #
    def _route(self, task_class: str, messages: list[dict], tools: list | None) -> list[ModelSpec]:
        specs = [s for s in CATALOG.get(task_class, CATALOG["simple"]) if s.status == "active"]
        # Downgrade: short single-turn prompts without tools do not need a frontier model.
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if task_class == "generation" and not tools and prompt_chars < 1_500:
            specs = CATALOG["simple"] + specs
        # Weighted shuffle among equal-priority entries for load balancing.
        head = [s for s in specs if s.weight > 1]
        if head:
            random.shuffle(head)
        return head + [s for s in specs if s.weight <= 1]

    def _is_unhealthy(self, spec: ModelSpec) -> bool:
        return self._unhealthy_until.get(f"{spec.provider}:{spec.model}", 0) > time.time()

    def _mark_unhealthy(self, spec: ModelSpec, err: Exception) -> None:
        status = getattr(getattr(err, "response", None), "status_code", None)
        cooldown = 30 if status in (429, 503) else 10
        self._unhealthy_until[f"{spec.provider}:{spec.model}"] = time.time() + cooldown

    # ---- provider adapters --------------------------------------------------------- #
    async def _call(self, spec: ModelSpec, messages, tools, max_tokens, temperature) -> GatewayResponse:
        if spec.provider in ("openai", "vllm", "azure"):
            return await self._call_openai_compatible(spec, messages, tools, max_tokens, temperature)
        if spec.provider == "anthropic":
            return await self._call_anthropic(spec, messages, tools, max_tokens, temperature)
        raise NotImplementedError(spec.provider)

    async def _call_openai_compatible(self, spec, messages, tools, max_tokens, temperature) -> GatewayResponse:
        body = {"model": spec.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if tools and spec.supports_tools:
            body["tools"] = tools
        headers = {"Authorization": f"Bearer {self.secrets.get(spec.provider, '')}"}
        r = await self.http.post(PROVIDER_ENDPOINTS[spec.provider], json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        tool_calls = [{"id": c["id"], "name": c["function"]["name"],
                       "arguments": json.loads(c["function"]["arguments"] or "{}")}
                      for c in msg.get("tool_calls", [])]
        usage = {"prompt_tokens": data["usage"]["prompt_tokens"],
                 "completion_tokens": data["usage"]["completion_tokens"],
                 "total_tokens": data["usage"]["total_tokens"]}
        return GatewayResponse(text=msg.get("content") or "", tool_calls=tool_calls, usage=usage,
                               model=spec.model, provider=spec.provider, cost_usd=self._cost(spec, usage))

    async def _call_anthropic(self, spec, messages, tools, max_tokens, temperature) -> GatewayResponse:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        convo = [m for m in messages if m["role"] != "system"]
        body: dict[str, Any] = {"model": spec.model, "max_tokens": max_tokens, "temperature": temperature,
                                "messages": self._to_anthropic_messages(convo)}
        if system:
            # Prompt caching: mark the stable system prompt as cacheable.
            body["system"] = [{"type": "text", "text": system,
                               **({"cache_control": {"type": "ephemeral"}} if spec.supports_prompt_cache else {})}]
        if tools and spec.supports_tools:
            body["tools"] = [{"name": t["function"]["name"], "description": t["function"]["description"],
                              "input_schema": t["function"]["parameters"]} for t in tools]
        headers = {"x-api-key": self.secrets.get("anthropic", ""), "anthropic-version": "2023-06-01"}
        r = await self.http.post(PROVIDER_ENDPOINTS["anthropic"], json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        text = "".join(b["text"] for b in data["content"] if b["type"] == "text")
        tool_calls = [{"id": b["id"], "name": b["name"], "arguments": b["input"]}
                      for b in data["content"] if b["type"] == "tool_use"]
        u = data["usage"]
        usage = {"prompt_tokens": u["input_tokens"], "completion_tokens": u["output_tokens"],
                 "total_tokens": u["input_tokens"] + u["output_tokens"],
                 "cache_read_tokens": u.get("cache_read_input_tokens", 0)}
        return GatewayResponse(text=text, tool_calls=tool_calls, usage=usage, model=spec.model,
                               provider="anthropic", cost_usd=self._cost(spec, usage))

    @staticmethod
    def _to_anthropic_messages(convo: list[dict]) -> list[dict]:
        out = []
        for m in convo:
            if m["role"] == "tool":
                out.append({"role": "user", "content": [{"type": "tool_result",
                                                         "tool_use_id": m["tool_call_id"],
                                                         "content": m["content"]}]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                blocks = ([{"type": "text", "text": m["content"]}] if m.get("content") else []) + [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                    for c in m["tool_calls"]]
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    # ---- helpers ------------------------------------------------------------------- #
    @staticmethod
    def _cost(spec: ModelSpec, usage: dict[str, int]) -> float:
        billable_in = usage["prompt_tokens"] - int(usage.get("cache_read_tokens", 0) * 0.9)
        return round(billable_in / 1000 * spec.input_cost_per_1k
                     + usage["completion_tokens"] / 1000 * spec.output_cost_per_1k, 6)

    @staticmethod
    def _cache_key(spec: ModelSpec, messages, tools, max_tokens, temperature) -> str:
        payload = json.dumps({"m": spec.model, "msgs": messages, "tools": tools,
                              "max": max_tokens, "t": temperature}, sort_keys=True, default=str)
        return "llm:cache:" + hashlib.sha256(payload.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# LiteLLM proxy equivalent (config.yaml) for teams that prefer an off-the-shelf gateway
# --------------------------------------------------------------------------- #
LITELLM_CONFIG_YAML = """
model_list:
  - model_name: reasoning
    litellm_params: { model: anthropic/claude-sonnet-4-5-20250929, api_key: os.environ/ANTHROPIC_API_KEY }
  - model_name: reasoning
    litellm_params: { model: openai/gpt-4.1-2025-04-14, api_key: os.environ/OPENAI_API_KEY }
  - model_name: simple
    litellm_params: { model: openai/meta-llama/Llama-3.1-8B-Instruct, api_base: http://vllm.internal:8000/v1 }
router_settings:
  routing_strategy: latency-based-routing
  num_retries: 2
  fallbacks: [{ reasoning: [simple] }]
  cooldown_time: 30
litellm_settings:
  cache: true
  cache_params: { type: redis, host: os.environ/REDIS_HOST }
  success_callback: [otel, langsmith]
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  max_budget: 5000
  budget_duration: 30d
"""
