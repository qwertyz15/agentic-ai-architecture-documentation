# Agentic AI Application Architecture — Engineering Design Documentation

Complete engineering design documentation for a production-grade Agentic AI application,
derived from the twelve-layer reference architecture diagram (`assets/architecture-diagram.png`).

Two deliverables are included:

1. **Interactive documentation website** — open `index.html` in any browser (no server required).
2. **PDF whitepaper** — `Agentic-AI-Architecture-Design-Document.pdf` (73 pages): *Design and
   Implementation of a Production-Grade Agentic AI Application Architecture*.

## Project structure

```
agentic-ai-architecture-documentation/
├── index.html                       # Chapter 1  – Introduction and scope
├── architecture-overview.html       # Chapter 2  – Request lifecycle, flows, Layer 1, request handling, design patterns
├── agent-runtime.html               # Chapter 3  – Layer 2: orchestrator, planner, sub-agents, memory manager, action executor
├── memory-system.html               # Chapter 4  – Layer 4: memory stores, RAG, memory engineering
├── tools-integrations.html          # Chapter 5  – Layer 3: tools, sandboxing, MCP
├── guardrails.html                  # Chapter 6  – Layer 5: input and output guardrails
├── llm-gateway.html                 # Chapter 7  – Layers 6 & 7: gateway, models, fine-tuning, self-hosting, LLMOps
├── observability.html               # Chapter 8  – Layer 8: logging, tracing, metrics, evaluation, dashboards
├── security.html                    # Chapter 9  – Layer 9: security controls, governance, compliance, threat model
├── deployment.html                  # Chapter 10 – Layers 10 & 11: infrastructure, CI/CD, performance engineering
├── human-in-the-loop.html           # Chapter 11 – Layer 12: approvals, feedback, active learning, escalation
├── reference-implementation.html    # Chapter 12 – Stack, end-to-end use case, challenges, tech map, roadmap, checklist, references
├── Agentic-AI-Architecture-Design-Document.pdf
├── README.md
├── assets/
│   ├── architecture-diagram.png     # Source architecture diagram
│   ├── diagrams/*.svg               # Rendered Mermaid diagrams (13)
│   ├── icons/                       # Favicon and logo
│   ├── style.css, site.js           # Site styling and behaviour (sidebar, search, scroll-spy)
│   └── search-index.js              # Generated full-text search index
├── diagrams/*.mmd                   # Mermaid sources for every diagram
├── code-examples/
│   ├── agent_orchestrator.py        # LangGraph orchestrator: planner, sub-agent ReAct loops, reflection, budgets
│   ├── memory_manager.py            # Redis + PostgreSQL/pgvector + Neo4j memory manager with budgeted context
│   ├── tool_executor.py             # Tool registry, MCP discovery, permissions, risk tiers, circuit breaker
│   ├── llm_gateway.py               # Routing, failover, caching, cost metering, provider adapters
│   └── guardrails.py                # Input/output guardrail pipelines (regex, classifiers, grounding judge)
└── tools/                           # Build tooling (Markdown sources, templates, build and PDF scripts)
```

## Using the website

- Open `index.html`. The left sidebar lists all chapters; the chevron expands each chapter's sections.
- Press `/` to focus search. Search covers every heading and paragraph across all pages.
- Each diagram has a "Mermaid source" toggle showing the `.mmd` file used to render it.
- On wide screens a right-hand rail tracks the current section.

The site is fully static and works from the file system, an S3 bucket or any web server.

## Diagrams

Sources live in `diagrams/`. To regenerate the SVGs after editing:

```bash
npm install -g @mermaid-js/mermaid-cli
for f in diagrams/*.mmd; do mmdc -i "$f" -o "assets/diagrams/$(basename "${f%.mmd}").svg" -b transparent; done
```

The site references the rendered SVGs, so no JavaScript library is needed at view time.

## Code examples

The five modules in `code-examples/` are reference implementations written against real library
APIs (LangGraph, Pydantic, Redis, asyncpg, httpx, jsonschema). They are intended to be read alongside
the chapters and adapted into a project rather than run as-is; each file's docstring lists its
dependencies and the design rules it demonstrates.

## Rebuilding the documentation

Content is written in Markdown under `tools/content/` (one file per chapter) and turned into both the
site and the print HTML by `tools/build.py`; `tools/render_pdf.py` renders the PDF with a paginated
table of contents (two-pass rendering with headless Chromium via Playwright).

```bash
pip install markdown pygments playwright pypdf && playwright install chromium
python tools/build.py        # site pages + tools/print.html
python tools/render_pdf.py   # PDF with TOC page numbers, headers and footers
```

Paths at the top of both scripts assume the repository is at `/home/claude/…`; adjust `ROOT`,
`CONTENT` and `TEMPLATES` for another location.

## Document metadata

- Title: Design and Implementation of a Production-Grade Agentic AI Application Architecture
- Subtitle: Architecture, Engineering Design, Security, Deployment and Operational Guidelines
- Version 1.0 — 26 September 2026
