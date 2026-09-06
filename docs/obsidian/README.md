# Obsidian vault — trading system architecture & workflow

## Opening this vault

In Obsidian: **Open folder as vault** → select `docs/obsidian/` (this
directory, not the repo root). No community plugins are required or used —
every diagram is native Obsidian Canvas (`.canvas`, plain JSON). This vault
has no personal notes in it; `.gitignore` keeps local Obsidian state
(`.obsidian/`, workspace layout, cache) out of git.

## What each Canvas is

| Canvas | Represents | Nature |
|---|---|---|
| `01-system-architecture.canvas` | Every service, data store and external API in the stack, and who can talk to whom. | 🟢 **Engineering ground truth** |
| `02-trading-cycle.canvas` | The trading agent's real decision procedure, step by step, plus the backtest-specific execution branch. | 🟢 **Engineering ground truth** |
| `03-project-workflow.canvas` | NOW (agreed implementation order) / NEXT / LATER (discussion items from `docs/PROJECT_BACKLOG.md`, not approved work). | 🟢 **Engineering ground truth** — a roadmap snapshot, not a plan Claude may execute unprompted |
| `04-linkedin-story.canvas` | Four screenshot-ready frames explaining the project for an outside, non-technical audience. | 🟡 **Presentation-oriented** — simplified for readability; scrubbed of any run ID, credential, URL or internal path |

Canvases 1–3 are the source to trust when reasoning about the real system.
Canvas 4 deliberately trades precision for clarity and is not a substitute
for 1–3.

## Keeping this vault current

Update the relevant Canvas (not just this README) after a change that
alters what Canvas 1 or 2 actually depicts: a new/removed service, a changed
communication path, a changed authoritative-state owner, or a real change to
the cycle's own step order or rules. Routine bug fixes, correction passes,
smoke tests and evaluation-result updates do **not** need a Canvas change —
only real architectural or procedural drift does.

**Canvas 3's NOW/NEXT/LATER placement changes require explicit user
approval before Claude edits them** — same rule `docs/PROJECT_BACKLOG.md`
already states for the backlog itself. Reflect a status change (e.g.
`discussing` → `approved`) only after the user has actually made that call,
not because a task looks reasonable.

## Not for this vault

Personal study material (e.g. the separate "Hermes Ledger" reading
referenced in `HANDOFF.md`) stays outside this repository entirely. This
vault is project documentation, not a personal notebook.
