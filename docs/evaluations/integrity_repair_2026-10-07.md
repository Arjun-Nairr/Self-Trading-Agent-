# Integrity repair: identity masking and technical-level provenance (2026-10-07)

**Why:** the evidence review of the July 2022 Laya-vs-random pilot
(`laya_vs_random_2022-07_2026-10-06.md` §11) found two defects:
- identifying cues survived masking, and the leak checker shared the
  masker's dictionary, so it could not see them;
- maverick's synthetic ±5%/±10% prices were presented as support and
  resistance, and every BUY stop sat on the synthetic −5% level.

This block repairs both in the backtest controller.

**Not changed:**
- risk numbers or strategy thresholds;
- RULES.md, AGENTS.md or SOUL.md;
- Laya and its settings, pools, manifests or alias seeds;
- model settings, the dashboard or live mode.

**No run** of any kind was made, and **no container needs a rebuild**:
maverick, trade-journal and the Laya worker image are unchanged.

## 1. What changed

### Identity masking (`run_backtest._BRAND_OVERRIDES`, `anonymize.NEUTRAL_REDACTIONS`)

- **Company variants → alias:** 7 symbols across the fixed-10 and 48-name
  pools gain verified variants.
  - **Categories:** former names, abbreviations, labels/subsidiaries and
    partial-name forms.
  - **Ambiguous single words stay unmasked:** for example, a word that
    also names an unrelated company.
- **Other parties → neutral role tokens**, never the candidate's alias:
  investors, partners, affiliates, separately listed subsidiaries and
  products, 12 terms in all. Example:
  `[REDACTED INVESTOR] boosts stake …`. The sentence's meaning, including
  adverse news, is kept.
- **Alias mappings are unchanged.** `_opaque_aliases` uses names only to
  avoid collisions, and none of the new variants can collide.

### Independent known-cue check (`identity_cues.py`, new)

- **Reference list:** a separately maintained list of 18 cues verified in
  stored prompts or by the earlier integrity audit (H4).
- **Residue check:** flags an alias left next to a word of its own legal
  name (`<alias> <Name-word>`), using the watchlist's legal names.
- **Independence:** it imports nothing from the masker (tested).
- **Where it runs:** on the **final rendered text**, at three points.

| Point | What it covers | On detection |
|---|---|---|
| Decision prompt, after `build_prompt` | cues and residues | the cycle halts before Hermes |
| Pre-fill gate prompt | cues and residues | the cycle halts; the order is neither confirmed nor treated as "no adverse news" |
| `_call_hermes` | cues, on every model call (decision, retry, gate) | raises before any subprocess |

### Observed-only technical levels (`run_backtest.fetch_technicals`)

- **Source:** the maverick script now returns indicator fields plus the
  same service's raw daily bars. **maverick's mixed `levels` are no longer
  read.**
- **Calculation:** `observed_levels_from_bars` computes the 30-session
  lowest low (S1) and highest high (R1) from bars dated on or before the
  decision date. Later bars are dropped; this is the point-in-time gate.
- **Provenance:** comes from that calculation, not from comparing values
  to percentages. A value equal to `close × 0.95` stays observed (tested).
- **When there are no levels:** no bars, a malformed or non-finite bar,
  no bar on the decision date, or fewer than 30 sessions all give
  "unavailable".
- **What Hermes sees:** `observed levels [lowest low / highest high of the
  last 30 sessions …; not pivot analysis]: S1 $… R1 $…`, or an explicit
  "unavailable — no evidenced stop, so no BUY". No calendar metadata.
- **Prompt change:** a STOP EVIDENCE instruction is added.
  `PROMPT_SCHEMA_VERSION` 3 → 4.

### Checkable stop basis (`stop_evidence`, in the run_day decision loop)

- **Rule:** a BUY's stop must equal one of that candidate's **supplied**
  observed support levels, to the cent. Otherwise it is rejected before
  queueing ("decision-time evidence check rejected -- §6a …").
- **No fallback:** no level is manufactured. With no in-band evidenced
  stop, there is no entry.
- **Where:** the check is controller-side; `risk_checker.py` is unchanged.
- **Record:** a private `candidate_status` entry records each proposed
  BUY's `stop_ref`/`stop_basis`, and the target's real basis: `R1`, or
  `derived_not_a_supplied_level`.
- **Not checked:** SELLs, held positions and mechanical exits.

## 2. Verification (offline)

| Check | Result |
|---|---|
| New regressions (`test_integrity_repair.py`) | 35, written first and failing, now passing |
| Backtest-harness suite | 593 passed |
| Same suite with Docker/Hermes subprocess calls blocked | 593 passed, 0 external calls attempted |
| `python verify.py` / `git diff --check` | 12/12 PASS / clean |
| Existing fixtures | technicals now built with `levels_payload`; sizing-scenario stops supplied as the observed low |

**The 35 regressions cover:**
- **Masking:**
  - every verified category (masked or neutrally redacted) in both
    populations;
  - case, punctuation and overlapping variants;
  - unaltered ordinary prose and unrelated companies.
- **Independent detection:**
  - it catches a cue the masker dictionary lacks, which `assert_clean`
    cannot;
  - residue detection.
- **Final-prompt checks:**
  - the decision prompt is checked before Hermes;
  - the gate prompt is refused rather than confirmed;
  - `_call_hermes` refuses before any subprocess.
- **Levels:**
  - observed-only provenance;
  - window and future-bar exclusion;
  - observed/percentage coincidence;
  - missing, short, stale, malformed and non-finite bars;
  - the adapter drops synthetic levels and raw bars.
- **Stop evidence:**
  - an unsupported stop is rejected;
  - an evidenced stop is queued when the other rules pass;
  - no in-band level means no entry;
  - malformed evidence is rejected;
  - derived targets are labelled;
  - SELLs and held positions are unaffected.

**Historical evidence, reconstructed offline:**
- **Method:** the July pilot's stored leak lines, with each alias returned
  to the variant the old masker had replaced, were re-masked through the
  repaired path.
- **Result:** **58/58 distinct lines are clean** (37 verified, 21
  speculative).
- **Stored prompts are unchanged:** rescanning them still shows the
  historical cues (9 Laya-arm prompts). That is the record of the defect,
  not a failed repair.
- **Levels:** the raw bars behind the July packets were never stored
  (hashes only), so level packets cannot be rebuilt offline. The adapter
  is tested with stubbed provider bars instead.

## 3. Scoped audit

**Masking path:** raw news → `mask_news`/`mask_text` → packet
`assert_clean` → final prompt → independent check → `_call_hermes` check.
- **Paths covered:** decision prompt, retry (through `_call_hermes`) and
  the pre-fill gate.
- **Also covered:** reflections and recent insights are part of the
  decision prompt, so they are inside the final check.
- **No other** backtest model-call path exists.

**Level path:** as-of bars → observed S1/R1 → prompt → proposed stop →
`stop_evidence` → queue or reject → `candidate_status`.
- **Replay:** a checkpoint replay uses the checkpointed `input_payload`, so
  the result is deterministic.
- **Legacy checkpoints:** a mid-cycle checkpoint written before this
  repair has no `level_evidence`. A BUY replayed from it is rejected with
  an explicit reason.

**Outside this block (reported, not changed):**
- **Live mode:** Hermes's own maverick support/resistance tool still
  returns the mixed synthetic levels. Fixing it needs a `maverick-mcp`
  source change and an image rebuild. Live-paper is not active.
- **Offline diagnostics:** `geometry_scan.py` and `counterfactual.py` read
  `support`/`resistance` lists, which are now observed values only.

## 4. Expected effect and limitations

**Expected effect:**
- **Fewer entries are expected.** A stop must now be the observed
  30-session low, inside the existing 3–12% band. Fixed percentage
  brackets, which every July BUY used, are no longer possible.
- **This is the intended honest behaviour**, not a regression to tune
  away. Its trading effect is unmeasured.

**Limitations:**
- **Masking:** known-cue protection is **finite**.
  - Unlisted brands, executives, counterparties and paraphrases can still
    identify a company, and prices themselves remain a fingerprint.
  - Speculative cues, such as sector peers named in a headline, are
    deliberately not redacted.
  - New cues should be added only with evidence.
- **Levels:**
  - one observed low and one observed high per name is minimal evidence,
    not pivot or support/resistance analysis;
  - the 1-cent equality rule is a checkability rule, not a risk number.
- **Past runs are not re-validated.** None of the earlier results,
  including the July pilot, become clean; their corrected caveats stand.

## 5. Readiness

The repaired inputs are ready for a **separately authorized acceptance
test** (for example, a short smoke episode). It needs Docker and no
rebuild. Suggested acceptance checks:
- stored prompts carry observed-level lines and schema 4;
- zero listed cues or residues reach a prompt;
- every queued BUY's `candidate_status` shows `stop_basis: observed_low`.
