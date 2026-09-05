# Content context

## Channel identity

- **Handle:** `@Lost-19-Year-Old`
- **Positioning:** A self-aware, "cooked" 19-year-old using AI tools to get genuinely good at computer science by building real projects in public.
- **Tone:** Relatable, candid, funny, and serious about learning. "Cooked" is the personality hook; observable learning, clear explanations, and real project evidence are the proof.
- **Scope:** Not only the trading project. Cover AI agents, trading systems, experiments, failures, fixes, and future software/CS projects.

## Content pillars

1. Build-in-public project diaries: what was attempted, what broke, and what changed.
2. Plain-English technical lessons grounded in actual project work.
3. Honest failure and revision stories: wrong assumptions, debugging, and redesigns.
4. AI-assisted learning/building: what the tools helped with and where judgment still mattered.

## Actual build workflow and visual language

- The user describes the workflow as **pure vibe coding** and wants that to be obvious in the content.
- Do not assume or depict VS Code, conventional manual coding, or footage implying the user personally typed the implementation line by line.
- Show the authentic loop instead: explaining a goal to AI tools, reviewing plans or diffs, running commands/tests, inspecting results, finding failures, and prompting the next revision.
- Frame AI use openly as the method through which the user is attempting ambitious projects and learning computer science, not as something to conceal.
- For voiceover-led videos, favor slow screen captures of the AI conversation, plans, diffs, test output, architecture, and dashboard over staged typing footage.

## Naming distinction — do not conflate these

- **Hermes is the underlying agent harness, not the name of the trading agent.**
- **The trading agent has not been named yet.** Refer to it as "the trading agent," "my AI trading agent," or "the project" until the user chooses a name.
- Never call the trading agent "Hermes" or present Hermes as the trading system's product name.

## Trading-project claim boundaries

- The trading project is a long-only, US-equities, paper-trading swing-trading system built on the Hermes agent harness.
- **Completed work:** a full 50-cycle backtest run ("Round 2") has finished, spanning 2026-02-17 through 2026-04-28. It produced 16 closed trades and 1 open position at that endpoint (+3.70% total return on a $100,000 paper account). Full detail: `docs/evaluations/round2_50cycle_evaluation_extract.md` and `docs/evaluations/round2_benchmark_sensitivity.md`.
- **Current evaluation:** that result has been benchmarked against cash, SPY buy-and-hold, an equal-weight watchlist buy-and-hold, and a simple technical baseline over the identical dates, plus a mechanical sensitivity pass over a few numeric rules. This is experimental backtest evidence about judgment quality on a small sample (16 trades) — **not proof of profitability, not a guarantee of future performance, and not financial advice.**
- **Planned, not yet started:** no live paper-trading deployment has begun. The six-month live/extended training run has not started. Any content must keep completed work (the finished backtest run), current evaluation (the benchmark/sensitivity analysis), and planned future work (live deployment) clearly distinct — never blur "backtested" into "live" or "evaluated" into "proven profitable."
- Do not imply profitability, financial advice, live deployment, or autonomous wealth generation.
- A good framing is: building a trading agent cautiously enough to test and reject its own bad decisions.

## Candidate framing

- Channel description: "A cooked 19-year-old trying to get good at computer science with AI tools. Building real projects in public, breaking things, fixing them, and documenting what actually works."
- Trading-agent video title: "A Cooked 19-Year-Old Tries to Build an AI Trading Agent."
