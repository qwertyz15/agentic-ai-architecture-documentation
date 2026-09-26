---
file: architecture-overview.html
title: Architecture Overview
nav: Architecture Overview
chapter: High-Level System Architecture
accent: "#1d5fd1"
summary: The complete request lifecycle, the four flows, Layer 1 (Users & Channels), Request Handling, and the agentic design patterns the architecture supports.
---

## Overview

The architecture is a layered, service-oriented system. Requests enter through Layer 1, are authenticated and shaped by the Request Handling block of Layer 2, screened by input guardrails (Layer 5), executed by the agent runtime, which draws on memory (Layer 4), tools (Layer 3) and models through the gateway (Layers 6 and 7), and leave through output guardrails back to the user. Layers 8 to 12 wrap around all of that.

A few architectural decisions shape everything downstream:

- **The runtime is separated from the models.** Agents never hold provider credentials or call a vendor API directly. All inference goes through the LLM gateway. This is what makes routing, failover, cost control and model versioning possible without touching agent code.
- **Tools are separated from agents.** Agents emit *intents* (structured tool calls). The action executor decides whether and how to carry them out. Permission, validation, retries and sandboxing live in the executor, not in prompts.
- **Memory is a service, not a prompt.** The memory manager assembles a bounded context bundle from several stores. Agents cannot silently grow the context or read another tenant's data.
- **Guardrails are on the boundary, twice.** Input guardrails run before any model sees user text; output guardrails run before any user sees model text. Tool results are treated as untrusted input as well.
- **Every step is a span.** The orchestrator, executor and gateway emit OpenTelemetry traces, so a single run is reconstructible end to end.

## The request lifecycle

{{diagram:system-flow|End-to-end request flow across the layers of the reference architecture}}

A typical request passes through the following stages. The numbers correspond to the sequence diagram below.

1. **Ingress.** A channel (web app, Slack bot, API client) sends an HTTPS request or opens a WebSocket. It carries a bearer token from the identity provider and a session identifier.
2. **Request handling.** The receiver authenticates the token, resolves roles and attributes, checks rate limits and quotas, validates the payload against a schema and loads or creates the session.
3. **Input guardrails.** Deterministic checks (schema, injection heuristics, secrets, PII) and model-based classifiers run. The request is rejected, masked or passed on with risk labels.
4. **Context assembly.** The orchestrator asks the memory manager for a context bundle: recent turns, a rolling summary, user preferences, semantically similar memories and related entities, trimmed to a token budget.
5. **Planning.** The planner calls a reasoning model through the gateway and returns a task graph: tasks, the sub-agent responsible for each, dependencies and acceptance criteria.
6. **Execution.** For each task the orchestrator runs a bounded ReAct loop in the appropriate sub-agent. Tool calls go through the action executor, which enforces permissions and may pause for human approval. Observations are written to memory.
7. **Reflection and synthesis.** A reviewer step critiques the aggregated results; if acceptable, a generation model writes the final deliverable.
8. **Output guardrails.** The draft is validated, checked for grounding against the collected evidence, masked for PII and screened for policy. Failed drafts are regenerated with feedback or replaced by a safe response.
9. **Delivery.** Tokens are streamed to the channel. The turn is persisted. Telemetry (trace, tokens, cost, guardrail verdicts) is exported.

{{diagram:sequence-lifecycle|Sequence diagram of a complete run, including the per-task ReAct loop and telemetry}}

## The four flows

The legend of the architecture diagram separates four kinds of interaction. Keeping them distinct in the implementation matters because they have different latency, reliability and security requirements.

{{table:The four flows and their implementation characteristics}}

| Flow | Direction | Transport | Requirements |
|---|---|---|---|
| Request flow | User → runtime → tools/models → user | HTTPS, SSE/WebSocket for streaming; internal gRPC or HTTP | Low latency, cancellation, back-pressure |
| Control flow | Orchestrator → planner → sub-agents; guardrails → runtime; gateway → providers | In-process (graph edges) or message queue for long runs | Determinism, bounded loops, resumability |
| Data flow | Runtime ↔ memory stores; services → observability | Database drivers, OTLP | Tenant isolation, consistency, retention |
| Feedback loop | Observability/HITL → datasets → prompts, policies, models | Batch pipelines, CI | Reproducibility, versioning |

**Decision flow** is a sub-case of control flow that deserves its own mention: at each ReAct step the model decides *continue / call tool / finish / escalate*. Those decisions are what the architecture must bound (max steps, token budget), record (tracing) and sometimes intercept (guardrails, HITL).

## Layer 1: Users & Channels

### Purpose

Layer 1 is every surface through which a request originates. The diagram lists web, mobile and desktop apps, Slack/Teams, API clients, voice/IoT and enterprise users. The layer's job is to present a consistent product experience over a single backend contract, so that channel-specific concerns (rendering, push notifications, voice transcription) never leak into the agent runtime.

### Supported interfaces

{{table:Channel types and their integration characteristics}}

| Channel | Typical stack | Auth pattern | Streaming | Notes |
|---|---|---|---|---|
| Web application | React / Next.js, TypeScript | OIDC authorization-code + PKCE | SSE or WebSocket | Primary surface; rich rendering of citations, tables, approvals |
| Mobile application | Flutter, React Native, Swift/Kotlin | OIDC + refresh tokens in secure storage | SSE with reconnection | Handle background/foreground and offline queues |
| Desktop application | Electron, Tauri | Same as web | WebSocket | Can host local MCP tools (file system, IDE) |
| Slack / Teams | Bolt SDK, Bot Framework | App-level token + user mapping | Message edits (progressive updates) | Thread = session; slash commands map to intents |
| API clients | REST/GraphQL SDKs | OAuth2 client-credentials or API keys | SSE | Programmatic access; strict rate limits and quotas |
| Voice assistants / IoT | ASR → text → agent → TTS; MQTT for devices | Device certificates, scoped tokens | Chunked audio | Latency budget tight; use small models for intent detection |
| Enterprise users | SSO via corporate IdP (Entra ID, Okta) | SAML/OIDC, group claims | Any | Group claims drive RBAC in Layer 2 |

### Frontend architecture

The frontend is a thin client. It owns rendering, local UI state and session tokens; it does not own conversation state (the backend does) and it never calls model providers. A typical Next.js layout:

- `app/(chat)/` – conversation view, streaming renderer, approval cards for HITL, citation viewer.
- `lib/api.ts` – typed client for the backend contract (`POST /v1/runs`, `GET /v1/runs/{id}/events`).
- `lib/auth.ts` – OIDC flow, token refresh, silent renew.
- `components/blocks/` – renderers for structured output blocks (markdown, table, chart, file, approval request).

The renderer must be able to display **partial** results. Agentic runs are long; users need to see the plan, which task is running and intermediate tool results, not a spinner for two minutes.

### API communication

Expose one small, stable contract:

```http
POST /v1/runs
Authorization: Bearer <access_token>
Content-Type: application/json

{ "session_id": "sess_8f2...", "input": "Analyse competitors and prepare a business report",
  "attachments": [], "options": { "stream": true } }

HTTP/1.1 202 Accepted
{ "run_id": "run_01J...", "events_url": "/v1/runs/run_01J.../events" }
```

```http
GET /v1/runs/run_01J.../events
Accept: text/event-stream

event: plan        data: {"tasks":[...]}
event: step        data: {"task":"t1","agent":"researcher","tool":"web_search"}
event: token       data: {"delta":"Acme's revenue grew"}
event: approval    data: {"tool":"send_email","arguments":{...},"approval_id":"apr_..."}
event: done        data: {"usage":{"total_tokens":48120},"cost_usd":0.61}
```

Separating *submit* from *subscribe* lets the run continue if the client disconnects, lets several clients (web and Slack) watch the same run, and maps directly onto a queue-backed worker model.

GraphQL is a reasonable choice when the frontend needs many shaped reads (session lists, run history, memory inspection); the run submission and event stream stay on REST/SSE either way.

### Authentication flow

1. The client redirects to the identity provider (Auth0, Keycloak, Entra ID, Okta, Cognito) using the authorization-code flow with PKCE.
2. The IdP returns an ID token and a short-lived access token (JWT, 5–15 minutes) plus a refresh token.
3. Every request carries the access token; the API validates signature, issuer, audience and expiry against the IdP's JWKS.
4. Group and role claims are mapped to the internal principal object used for authorization and tool permissions.
5. Service-to-service calls inside the platform use workload identity (Kubernetes service accounts with SPIFFE/SPIRE or cloud IAM), never user tokens.

### Session management

A *session* is the unit of conversational continuity; a *run* is one request inside it. Sessions are stored server-side (Redis for hot state, PostgreSQL for history) and referenced by opaque IDs. The client never sends conversation history; it sends the session ID and the new input. This keeps the context window under backend control and prevents clients from injecting fabricated history.

### Real-time streaming

Server-Sent Events are the default: they are unidirectional, work through most proxies, reconnect natively (`Last-Event-ID`) and are trivial to consume. WebSockets are used when the client needs to send mid-run input (cancel, answer a clarification, approve an action). Both must survive load balancers: set idle timeouts above the longest expected pause and send heartbeat comments every 15–30 seconds.

## Request Handling (Layer 2, ingress block)

The Request Handling block of the orchestration layer contains six components. They are shown inside Layer 2 because they are the runtime's front door, but they are typically deployed as a separate stateless API service in front of the agent workers.

### Request receiver

Responsibilities: terminate the API contract, route by path and version, enforce payload limits, assign a request/run ID, attach tracing context, and enqueue the run. Implementation: FastAPI (Python) or NestJS (TypeScript) behind an API gateway (AWS API Gateway, Kong, Envoy, Azure API Management) that handles TLS, WAF and coarse rate limiting.

### Session manager

Loads or creates the session, validates that the caller owns it, and provides the session handle to the memory manager.

{{table:Session storage options}}

| Store | Use for | Why |
|---|---|---|
| Redis | Hot session state, recent turns, locks, rate-limit counters | Sub-millisecond, TTL support, atomic operations |
| PostgreSQL | Durable session and run history, audit | Transactions, relational queries, retention policies |
| DynamoDB | Same as PostgreSQL in serverless/AWS-native designs | Managed scaling, TTL attributes |

Guard against concurrent runs on the same session with a Redis lock (`SET session:{id}:lock run_id NX PX 300000`), otherwise two runs will interleave memory writes.

### Authentication

Validate JWTs locally with cached JWKS; never call the IdP per request. Support OAuth2 client-credentials for machine clients, and SSO (SAML or OIDC federation) for enterprise tenants. Reject tokens with missing `aud` or with an `iss` not in the tenant's allow-list.

### Authorization

RBAC answers *what role can do*; ABAC answers *under which conditions*. Agentic systems need both because tool permissions depend on data attributes (a finance analyst may read the ledger for their own cost centre only).

```yaml
# Example policy (Cedar / OPA style)
permit(principal in Role::"analyst", action == Action::"call_tool", resource == Tool::"query_ledger")
  when { resource.args.cost_center in principal.cost_centers };
forbid(principal, action == Action::"call_tool", resource in ToolGroup::"irreversible")
  unless { context.approved_by != null };
```

Open Policy Agent (Rego), AWS Cedar or Casbin evaluate such policies in microseconds and keep authorization logic out of application code.

### Rate limiting

Three limits at three levels:

- **Edge**: requests per second per IP/API key (API gateway or Envoy).
- **Application**: runs per user per minute and concurrent runs per session (Redis token bucket, `INCR` + `EXPIRE` or a Lua script).
- **Cost**: tokens or dollars per tenant per day, enforced by the LLM gateway.

Cost limits are the ones that matter most: a single agentic run can consume 100× the tokens of a chat turn, so request-count limits alone do not prevent abuse.

### Request validation

Validate with a schema (Pydantic, Zod) before the payload reaches any model: type, length, allowed attachment MIME types, allowed options. Reject early with a 4xx; malformed input should never reach the guardrails, let alone the planner.

## Agentic design patterns

The runtime in Layer 2 is general enough to host several patterns. Choosing the right one is the first design decision for a given use case.

{{table:Agentic design patterns and when to use them}}

| Pattern | Mechanism | Strengths | Weaknesses | Use when |
|---|---|---|---|---|
| ReAct agent | Interleaved *Thought → Action → Observation* loop | Simple, adapts to observations, works with any tool-calling model | Greedy; can loop; no global plan | Single-agent tasks with unknown path, ≤ 10 steps |
| Planning agent (plan-and-execute) | Explicit plan first, then execute steps, re-plan on failure | Predictable, parallelisable, auditable | Plan may be wrong; extra latency | Multi-step tasks, cost-sensitive, needs approval of a plan |
| Reflection agent | Generator + critic loop (self-refine, reviewer model) | Higher quality, catches errors and hallucinations | Doubles cost; can over-refine | Reports, code, anything with quality bar |
| Multi-agent collaboration | Orchestrator + specialists sharing state | Separation of concerns, specialised prompts/tools/models | Coordination overhead, harder debugging | Heterogeneous tasks (research + analysis + code) |
| Tool-using agent | Model with function calling and a curated tool registry | Grounded actions, deterministic side effects | Tool sprawl, injection via tool output | Almost always; foundation for all others |
| Workflow agent | Deterministic graph with LLM nodes at fixed points | Reliable, testable, cheap | Not autonomous; cannot handle novel paths | Well-understood processes (ticket triage, document extraction) |

In the reference implementation the orchestrator is a *planning* agent, each sub-agent is a *ReAct* agent bounded by budgets, and a *reflection* step precedes synthesis. Workflow-style graphs are used where the process is known (for example, the RAG ingestion pipeline).

## Best practices

- Version the API contract from day one (`/v1/`). Agent behaviour will change weekly; the contract should not.
- Return `202 Accepted` and stream events; never hold an HTTP request open for a multi-minute run.
- Keep channels dumb. If Slack needs a different response format, add an output formatter in the action executor, not logic in the bot.
- Enforce cost quotas at the gateway and surface remaining budget to the user; blocked-for-budget is a better failure than a surprise invoice.
- Reject invalid input at the edge; log the rejection reason without logging the payload if it might contain secrets.
