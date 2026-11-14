# AI SDLC: project context

Inspected 2026-11-14. This is a source navigation index, not proof of runtime behavior.

## Start and scope

Python SaaS under src/, with apps/web/ as the web UI. Keep service work separate from project_templates/ and spikes/. Inspect the relevant tests and evals before changing orchestration.

Open this same project folder in Codex or Antigravity to share the files. Existing startup instructions and narrower module rules take precedence. Do not apply this profile to an independent nested repository.

## Source entry points

- `pyproject.toml`
- `apps/web/package.json`

## Verification selection

The entries below were found in source, not executed during harness installation. Inspect scripts and lifecycle hooks, choose the changed module, confirm local tools, and keep database/provider/network checks isolated and authorized.

| Working directory | Check entry point | Source / limitation |
| --- | --- | --- |
| `.` | `python -m pytest` | pyproject.toml pytest settings; inspect fixtures/options first |
| `.` | `python -m ruff check .` | pyproject.toml Ruff settings; check-only |
| `.` | `mypy configured; select targets and flags from existing project guidance` | pyproject.toml mypy settings; no inferred target |
| `apps/web` | `npm run build` | apps/web/package.json scripts.build |

## Improvement and handoff

For recurring failures, user corrections, or an explicit workflow improvement request, read `.agents/skills/harness-ai-sdlc/SKILL.md`. Keep ordinary small tasks direct. Record only evidence-supported, reusable project lessons in `.harness/lessons.md`.

Keep existing plans/changelogs/handoffs as the active-work source. The skill describes an optional shared handoff when no project convention exists. Recheck stale evidence when switching tools. Safety, permissions and acceptance criteria are fixed boundaries, not optimization candidates.

This adapts Propose-Probe-Compose and reflection from https://github.com/google-research/harness2 without installing its benchmark runtime or changing model, hooks, sandbox, credentials, CI or deployment configuration.
