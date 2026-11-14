# P0 — License Review & Third-Party Notices

- **Document ID:** `DOC-P0-LICENSES-001`
- **Owner:** Legal / Engineering (`platform-core`)
- **Date:** 2026-11-13

## 1. Upstream Reference Projects & License Compatibility

| Upstream Component | Source / Pinned Reference | License | Commercial SaaS Compatibility | How Used in AI SDLC SaaS |
| --- | --- | --- | --- | --- |
| **Deep Agents** | `langchain-ai/deepagents` (`0.7.36`) | MIT | Compatible (permissive) | Option 1A coding agent runtime + `BaseSandbox` adapter protocol |
| **Open SWE** | `langchain-ai/open-swe` (`5308cc586a9f8bbc1f8f7a83732b20a8cdf46211` pre-MDA reference; `99308297` studied) | MIT *(excluding `vendor/`)* | Compatible (only prompt/architectural patterns reused; zero `vendor/` code copied) | Architectural inspiration for planner/sandbox adapter & coding prompts |
| **MetaGPT** | `FoundationAgents/MetaGPT` (`0.8.2`, `11cdf466d042aece04fc6cfd13b28e1a70341b1f`) | MIT | Compatible | Clean-room isolated `ActionNode` + `[CONTENT]` parser + `ProductManager`/`Architect` roles (avoids broken `lancedb==0.4.0` PyPI tree) |
| **ChatDev 2.0** | `OpenBMB/ChatDev` (`main` @ `464a681ba7bda869db97a50c5fa707c9a0fb57a0`) | Apache-2.0 | Compatible (requires NOTICE/license attribution) | Pinned YAML workflow definitions & isolated `Critic`/`QA`/`CodeReviewer` workflow service |
| **InstaBox SDK** | `instabox-sdk` (`0.5.2`) | Commercial SDK / Apache-MIT wrapper (`httpx`) | Compatible for backend SaaS integration per provider ToS | Sandbox VM lifecycle adapter (`InstaBoxSandbox`) |

## 2. Core Platform Dependencies (Backend & Frontend)

- **Backend Python:** FastAPI (MIT), Uvicorn (BSD-3), Pydantic (MIT), SQLAlchemy (MIT), Alembic (MIT), LangGraph & LangChain Core (MIT), OpenAI SDK (Apache-2.0), HTTPX (BSD-3), PyJWT (MIT), Cryptography (Apache-2.0 / BSD-3), Bcrypt (Apache-2.0), PyGithub (LGPL-3.0 — note: dynamic import / HTTP REST `httpx` preferred over LGPL static bundling; control-plane GitHub adapter uses `httpx` REST directly so core remains cleanly permissive!), GitPython (BSD-3), Prometheus Client (Apache-2.0).
- **Frontend JS/TS:** React (MIT), React DOM (MIT), TypeScript (Apache-2.0), Vite (MIT), Lucide Icons (ISC).

> **Important License Note:** Using `httpx` directly for GitHub App JWT + REST API calls avoids LGPL distribution/compliance friction from `PyGithub` in packaged commercial distributions.
