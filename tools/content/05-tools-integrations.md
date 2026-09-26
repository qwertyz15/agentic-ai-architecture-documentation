---
file: tools-integrations.html
title: Tools & Integrations
nav: Tools & Integrations
chapter: Tools & Integrations Layer
accent: "#d6501b"
summary: Layer 3: search engines, databases, APIs, enterprise systems, sandboxed code execution, file storage and messaging, and the Model Context Protocol.
---

## Overview

Layer 3 is where the agent touches the world. The diagram lists seven integration families: search engines, databases (SQL, NoSQL, vector), APIs and web services, enterprise systems (CRM, ERP, HRMS), a code execution environment, file storage and email/messaging/notifications.

The layer's contract with the runtime is simple: every integration is exposed as a **tool** with a name, a description, a JSON Schema for arguments, a risk tier and an allow-list of agents. The action executor in Layer 2 is the only caller. Nothing in this layer is reachable from a prompt directly.

## Architecture

Tools reach the runtime through one of three mechanisms:

1. **Local tools**: Python functions registered in-process (`@tool` decorator in `code-examples/tool_executor.py`). Best for thin wrappers and latency-sensitive calls.
2. **MCP servers**: out-of-process servers that publish tools over the Model Context Protocol. Best for reusable, independently deployed integrations.
3. **Remote services**: the sandbox and heavy integrations run as separate services called over HTTP/gRPC; they are wrapped as local tools or MCP servers.

### Search engines

- **Web**: Google Programmable Search / Vertex AI Search, Bing Web Search API, Brave Search API, Tavily and Exa (LLM-oriented, return cleaned content), SerpAPI.
- **Enterprise**: Elasticsearch / OpenSearch, Azure AI Search, Glean-style connectors; the vector store's hybrid search.
- Return normalised results (`title`, `url`, `snippet`, `published`), cap at 5–10 per call and fetch page content in a second tool (`read_url`) with size limits and an HTML-to-text pass. Search output is a prime injection vector; sanitise it.

### Databases

- **SQL** (PostgreSQL, MySQL, SQL Server, Snowflake, BigQuery): expose *read-only* query tools with a dedicated database role, statement timeout, row limit and, preferably, a curated set of parameterised queries or views rather than free-form SQL. If text-to-SQL is required, validate the generated SQL (parser, allow-listed tables, no DDL/DML) and run it under a read-only role.
- **NoSQL** (MongoDB, DynamoDB, Cosmos DB): same principle; expose query templates.
- **Vector databases**: exposed as `retrieve_documents` with ACL filtering (see [Memory System](memory-system.html)).

### APIs and web services

Any REST or GraphQL API can become a tool. Two approaches:

- **Hand-written wrappers**: a function per operation with a tight schema and a good description. Highest quality; scales to dozens of tools.
- **Generated from OpenAPI**: import the spec, generate one tool per operation, then prune. Fast; but generic descriptions and huge parameter schemas degrade tool selection. Curate.

Function calling is the mechanism: the model returns `{"name": "...", "arguments": {...}}`; the executor validates and invokes. Prefer tools that do one thing with few arguments; the model selects among them far more reliably than among ten flags on one tool.

### Enterprise systems

CRM (Salesforce, HubSpot, Dynamics), ERP (SAP, Oracle, NetSuite), HRMS (Workday, SuccessFactors), ITSM (ServiceNow, Jira). Considerations:

- Authenticate as the **user** (OAuth on-behalf-of, delegated tokens) so the system's own authorization applies; fall back to a service account with a policy layer only where delegation is impossible.
- Separate read tools from write tools; writes are `write` or `irreversible` risk tier and go through approval.
- Map enterprise identifiers explicitly (account IDs, employee IDs) rather than letting the model guess from names.
- Rate limits of the target system become tool-level concurrency limits.

### Code execution environment

The Coder agent needs to run code; the Analyst needs calculations. Running model-generated code is the most dangerous capability in the system and must be isolated.

{{table:Sandbox isolation options}}

| Technology | Isolation | Start-up | Fit |
|---|---|---|---|
| Docker container (plain) | Namespace/cgroup; shares kernel | ~1 s | Development only; kernel escape risk |
| gVisor (runsc) | User-space kernel | ~1 s | Good default on Kubernetes (RuntimeClass) |
| Kata Containers | Lightweight VM per container | 1–3 s | Strong isolation with container UX |
| Firecracker microVM | Minimal VM | ~150 ms | Best isolation/latency trade-off; used by managed sandboxes (E2B, Modal) |
| WebAssembly (Wasmtime, Pyodide) | Language-level sandbox | ~ms | Restricted languages/libraries; excellent for pure computation |
| Managed sandbox services | Provider-operated microVMs | ~200 ms | Fastest path to production; data leaves your boundary |

Baseline controls regardless of technology: no outbound network (or an egress allow-list), CPU/memory/time limits, read-only base image, ephemeral writable workspace, non-root user, seccomp profile, output size cap, one execution per sandbox instance (no reuse across users), and audit of every execution with its code.

### File storage

S3, Azure Blob Storage, Google Cloud Storage for uploads, generated artefacts (charts, reports, code) and ingestion sources. Tools: `read_file(path)`, `write_file(path, content)` scoped to a per-run or per-user prefix; presigned URLs (short TTL) to hand files to the frontend; server-side encryption with KMS keys; lifecycle rules for retention. Never give the agent a bucket-wide credential.

### Messaging systems

Email (SES, SendGrid, Microsoft Graph), Slack (Web API, Block Kit), Teams (Graph, Adaptive Cards), push and SMS (Twilio, FCM). These are **write** tools with real-world consequences: template the content where possible, require approval for external recipients, rate-limit per user, and record every send in the audit log with the approving identity.

## Model Context Protocol (MCP)

MCP is an open protocol (JSON-RPC over stdio or HTTP) through which a server exposes **tools**, **resources** (readable data) and **prompts** to any compatible client. It matters for an enterprise architecture because it turns integrations into a product: one MCP server for Jira, built once, is usable by every agent, every framework and every vendor's client.

### Why MCP is important

- **Decoupling.** Integrations are versioned and deployed independently of agents. A change to the CRM API touches one server.
- **Discovery.** Clients call `tools/list` at start-up and receive names, descriptions and JSON Schemas; the registry populates itself.
- **Portability.** The same server works with LangGraph, the OpenAI Agents SDK, Claude, IDEs and internal tools.
- **Ecosystem.** Vendors and the community publish servers for GitHub, Slack, Postgres, Google Drive, filesystems and more; enterprises publish internal ones.

### MCP servers in this architecture

Each integration family in Layer 3 can be an MCP server: `mcp-search`, `mcp-postgres-ro`, `mcp-crm`, `mcp-sandbox`, `mcp-storage`, `mcp-messaging`. They run as Kubernetes deployments with their own identities and secrets. The tool executor discovers them and namespaces their tools (`crm__get_account`).

### Tool discovery and permission control

Discovery is automatic; **trust is not**. The executor applies:

- An allow-list of MCP servers per environment (a new server does not become callable by being reachable).
- Risk-tier overrides per tool (`crm__update_opportunity` is `write` even if the server does not say so).
- Per-agent allow-lists and ABAC policy on arguments.
- Authentication to servers with per-server credentials and, where supported, the user's delegated token forwarded in the call context.
- Sanitisation of results: MCP tool results and resources are untrusted content.

Governance-side, the diagram places "MCP / Tool Permissions" in Layer 9; the policy is owned there and enforced in the executor.

## Implementation guide

- Start with a curated registry of 5–15 tools per agent; add more only with evaluation evidence.
- Write descriptions for the model, not for humans: what the tool returns, when to use it, when not to.
- Make argument schemas strict (`additionalProperties: false`, enums, max lengths).
- Assign a risk tier to every tool; treat unknown as `write`.
- Set timeouts per tool; agents wait synchronously and a hung tool stalls a run.
- Implement retries only for idempotent reads; a retried email is two emails.
- Add a circuit breaker per tool so a failing dependency degrades the agent instead of the platform.
- Log every call with arguments (masked), latency, result size and status.

## Example implementation

Registering local tools and discovering an MCP server (from `code-examples/tool_executor.py`):

```python
registry = build_registry()                       # web_search, run_code, send_email
crm = MCPServer("crm", "https://mcp-crm.internal", token=secrets["MCP_CRM_TOKEN"],
                default_risk="read", allowed_agents={"researcher", "analyst"})
await crm.discover(registry, risk_overrides={"update_opportunity": "write"})
executor = ToolExecutor(registry, approvals=approval_service, audit=audit_log, policy=opa_policy)
```

A minimal MCP server (Python SDK):

```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("crm")

@mcp.tool()
def get_account(account_id: str) -> dict:
    """Return the CRM account record (name, tier, ARR, owner) for a given account ID."""
    return crm_client.accounts.get(account_id)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

Sandbox execution request:

```json
POST /execute
{ "language": "python", "code": "import pandas as pd; ...",
  "limits": { "cpu_s": 30, "memory_mb": 512, "network": false, "disk_mb": 200 },
  "files": [ { "name": "competitors.csv", "url": "s3://.../presigned" } ] }
```

## Best practices

- **Common mistakes.** Free-form SQL with a privileged role; agents holding long-lived API keys; tool output pasted into the prompt unbounded; one tool with twenty optional parameters; sandboxes with network access; retrying writes.
- **Optimisation.** Return compact, structured results; paginate; pre-filter server-side; cache read tools with short TTLs; run independent tool calls concurrently.
- **Security.** Least privilege per tool, per agent, per user; delegated identity where possible; approval for writes; sanitise and bound all results; audit everything; treat every MCP server as a third-party dependency with review, pinning and network policy.
