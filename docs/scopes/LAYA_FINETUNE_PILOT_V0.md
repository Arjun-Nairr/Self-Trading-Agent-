# Laya review-priority fine-tuning: pilot scope and readiness (v0, 2026-10-05)

**Status: preparation only. Human label review is required. Training is
not approved.**

Nothing was trained, downloaded, provisioned, scored or deployed for this
document. The production selector, question, worker, rules and watchlists
are unchanged.

**Scope:**
- **Task:** a narrow review-priority classifier for the existing Laya
  selector.
- **Not in scope:** returns, trade quality or buy signals.

**Built offline:**
- the proposed rubric (§2);
- a 24-example private pilot with a review sheet (§3);
- a dataset and evaluation design (§4–5);
- a training-readiness map against the pinned package (§6);
- one helper module, `backtest-harness/laya_ft_pilot.py`, with 6 tests.

**Evidence base:**
- `laya_vs_controls_2026-10-04.md`: Laya is not shown to differ from
  random selection.
- `laya_framing_2026-10-04.md`: no framing or label variant helps, and
  there is label bias.
- The pinned package README calls the base checkpoints "a fast base to
  specialise, not a zero-shot decision engine".

## 1. Task definition

**Question:** given only this company-day's seven numeric card fields, does
it warrant a further technical/news review by Hermes?

**What it is:** the production question
(`laya_adapter.REVIEW_QUESTION`, `noul-criteria-v2`), unchanged.

**Two different things, kept apart:**
- **Unusual technical activity.** The card shows a notable move,
  stretch, oscillator extreme or volume burst. This is the task, and it is
  labelled direction-neutral, as the production criteria are.
- **A promising long-only setup.** This needs support and resistance
  levels, a stop inside RULES.md §6a, a target and reward:risk. None of
  these is on the card. Hermes judges them from full technicals.
  - These labels must not claim "promising".
  - Each example records a `direction` (up / down / mixed / none), so a
    later decision to deprioritise down-only activity needs no relabelling.

**What the seven fields cannot support** (no new data sources were added):

| Limitation | Consequence for labels |
|---|---|
| No market or sector context | Cannot tell an idiosyncratic −6% week from a market-wide one. Labels mean "unusual for this stock's own numbers", not "unusual vs. the market". |
| No price levels, longer trend, gaps or intraday data | "Good long setup" is unlabelable (above). |
| No liquidity or dollar volume, and no event flags (by design) | A 0.3× volume day could be a half-session or a data gap. This is flagged as ambiguous, not decided. |
| MACD direction saturates | On the 153 explored real cards, |MACD dir| ≥ 0.9 on 54 (35%). It carries little extra information at the extremes, so it only supports other signals and never decides. |
| Realized volatility includes the recent move | A vol-scaled move is useful, but it is partly circular. |
| SMA distance and range position are highly correlated | They count as one "location" family. |

**Labels are independent of:**
- future prices and outcomes;
- company identity and dates (the reviewer sees seven numbers only);
- Laya scores;
- Hermes decisions;
- prior shortlists.

The pilot builder never reads the score fields of its source file. Its
sample key uses only session and row ID (§3; tested).

## 2. Labelling rubric `review-rubric-v0.1-proposed`

**Status:** proposed until human review. It is implemented as
`laya_ft_pilot.rubric_reference` so that it can be audited and used as a
control.

### Fields and units

All are as built by `laya_cards.py`:

| Field | Unit / meaning | Ordinary looks like |
|---|---|---|
| 5d return | Fraction; close vs the close 5 sessions earlier | within ±2–3% |
| vs 20d avg | Fraction; close vs its 20-day SMA | within ±2–3% |
| RSI14 | 0–100, Wilder; 50 is balanced | 40–60 |
| MACD dir | tanh(MACD(12,26,9) histogram / price / 0.005), −1 to +1 | small |
| Relative volume | × today vs the mean of the prior 20 sessions | 0.7–1.4× |
| Annual volatility | Annualized stdev of the last 20 daily log returns | context only |
| 20d range position | 0 = 20-day low, 1 = 20-day high | 0.2–0.8 |

**Derived:** move vs own volatility = 5d return / (volatility × √(5/252)),
i.e. the 5-day move in units of its own 5-day standard deviation.

### Anchors by family

Correlated fields share one family.

| Family | Strong | Moderate |
|---|---|---|
| Move | \|5d\| ≥ 7%, or \|move vs own vol\| ≥ 2.5 with \|5d\| ≥ 4% | \|5d\| ≥ 4% |
| Location | \|vs 20d avg\| ≥ 7% | \|vs 20d avg\| ≥ 4%; range ≥ 0.90 or ≤ 0.10 |
| Oscillator | RSI ≤ 25 or ≥ 75 | RSI ≤ 32 or ≥ 68 |
| Participation | rel vol ≥ 2.5× | rel vol ≥ 1.6× |
| Momentum | supporting only | \|MACD dir\| ≥ 0.6 is noted, never counted |

### Labels

Apply these rules in order:

1. **Ambiguous** if the counted signals point in conflicting directions.
   Example: down 6% in five days but still 4.5% above the 20-day average.
2. **Review-worthy** if any strong signal is present.
   - Exception: a lone strong signal within 10% of its anchor is
     **ambiguous** (near threshold). RSI distance is measured from 50.
3. **Review-worthy** if moderate signals appear in two or more families,
   in a coherent direction.
4. **Ordinary / lower priority** if moderate signals appear in one family
   only.
   - Exception: within 10% of a strong anchor, the card is **ambiguous**.
5. **No counted signal:**
   - **ambiguous** if annual volatility ≥ 60% or rel vol ≤ 0.4×, because
     an unexplained single extreme could be recent turbulence, a
     half-session or a data issue;
   - otherwise **ordinary**.

**Ambiguous is a dataset-review state, not a model output.** It is never a
training label (the converter refuses it). An item still ambiguous after
adjudication becomes an evaluation slice.

### Borderline cases reviewers should expect

- **Several values just inside their anchors.** The rules may say
  ordinary where a reviewer sees a quiet slide to an extreme. This is the
  only case where the AI departed from the rules in the pilot.
- **Volume burst with a flat price.** Review-worthy by rule; possibly
  index rebalancing.
- **Reversal.** A bounce off the 20-day low has conflicting directions, so
  it is ambiguous by rule. Reviewers decide whether reversals count as
  notable.
- **High-volatility name moving 3–4%.** The vol-scaled move makes this
  ordinary. A low-volatility name moving 4–6% can be strong.

**The rubric is mostly a formula.** Rules 1–5 are deterministic. If
reviewers simply apply them, a fine-tuned Laya would **distil this
formula**, not demonstrate better judgement. In that case the formula
itself, with zero inference cost, is the better selector (§5).

## 3. Pilot (private, git-ignored)

**Location:** `backtest-harness/.laya_ft_pilot/<run>/`, built with
`python backtest-harness/laya_ft_pilot.py build --overrides <private json>`.

### Real cards: 12

- **Source:** the saved, hash-verified cards of the Laya-vs-controls run.
  - `private_results.json`, SHA-256 `8d140f99…08fa`;
  - three sessions × 51 cards, with per-day input digests re-checked.
- **Sampling method** (fixed in code before any label was assigned): per
  session in date order, take the 4 cards with the smallest
  `sha256("laya-ft-pilot-v1|session|row_id")`, skipping companies already
  drawn. The result is 12 cards from 12 different companies.
- **What the method never reads:** scores, ranks, shortlists, card values
  or outcomes.
- **These sessions are development material:** already explored, never a
  final test.

### Synthetic cards: 12

The synthetic cards are listed in `laya_ft_pilot.SYNTHETIC`:
- 3 ordinary;
- 4 clear;
- 5 borderline.

### What each record carries

- opaque `example_id`;
- `source_type`;
- the seven original values;
- a one-line rendering with units;
- `proposed_label` and the rules' `reference_label`;
- `direction`;
- a rationale and ambiguity flags;
- `label_status: ai_proposed_unreviewed`;
- provenance;
- `rubric_version`;
- a company-group key;
- for real cards only, the session and row ID (private).

### Provisional labels

| | Review-worthy | Ordinary | Ambiguous |
|---|---|---|---|
| Real (12) | 4 | 4 | 4 |
| Synthetic (12) | 5 | 3 | 4 |

**Provenance:** these labels are AI-proposed. Claude applied the reference
rules and checked each card, with one recorded judgement override (a real
card, ordinary → ambiguous). They are not independent ground truth and
were not expert-validated. Agreement with them is not evidence of model
quality.

**Rubric effect seen on the 153 explored cards:** 55 review-worthy, 88
ordinary, 10 ambiguous. That is 17–20 review-worthy per day, more than the
10 shortlist slots.

### Synthetic examples (public)

| Situation | Card | Proposed |
|---|---|---|
| Quiet | 5d +0.40%, vs avg +0.30%, RSI 51, MACD +0.05, vol 0.98×, ann vol 22%, range 52% | ordinary |
| Breakout on volume | 5d +9.0%, vs avg +7.5%, RSI 74, MACD +0.75, vol 2.8×, ann vol 38%, range 98% | review-worthy (up) |
| Volume spike, flat price | 5d +0.8%, vs avg +0.5%, RSI 53, MACD +0.10, vol 3.4×, ann vol 24%, range 60% | review-worthy |
| Pullback after a run-up | 5d −6.0%, vs avg +4.5%, RSI 52, MACD −0.45, vol 1.2×, ann vol 35%, range 62% | ambiguous (mixed) |
| Lone move near anchor | 5d +6.8%, vs avg +3.0%, RSI 63, MACD +0.40, vol 1.4×, ann vol 30%, range 85% | ambiguous (near threshold) |

### Review sheet `review_template.csv`

No model predictions, dates, sources or identities appear in it.

For each row, the reviewer:
1. reads the card;
2. fills `your_label_before_reading_proposal` (review_worthy / ordinary /
   ambiguous) and a note;
3. only then reads the proposal;
4. records accept / change / ambiguous, the `final_label`, their name and
   the date.

Agreement is computed on the blind column.

## 4. Smallest credible larger dataset (design; not built)

### Sampling unit

The unit is a complete session-day: every eligible card in the pool that
day. Whole days are needed so that top-10 coverage is measurable.

- **Sessions:** spread over calendar regimes from the available price
  history (for example 2022–2026).
- **Card building:** the existing point-in-time builder
  (`fetch_card_history` → `build_day_cards`; bars ≤ the session date).
- **Pool:** the same coverage-qualified 51-name pool. Its
  survivorship bias is inherited and must be stated.
- **Training days only:** may be enriched with extra borderline days
  chosen from card features, with the sampling weights recorded.
- **Test days:** never enriched.

### Starting size (estimate, not a requirement)

About 30 sessions × ~50 cards ≈ 1,500 cards:

| Partition | Sessions | Cards |
|---|---|---|
| Train | 16 | ~800 |
| Validation | 4 | ~200 |
| Calibration | 4 | ~200 |
| Test | 6 | ~300 |

- **Reasoning:** at about 35% prevalence, the test set holds about 100
  review-worthy cards, enough to estimate recall to roughly ±6 points.
- **Upstream scale for comparison:** a 1,200-case training split.
- **Labelling effort:** two reviewers at about 20–30 s per card is roughly
  8–13 h each. This is unmeasured and should be timed in the pilot.

### Review and disagreement

1. **Two independent reviewers.** At least one is not the rubric's author,
   and neither is the AI. Both are blind to each other, to sources, dates,
   identities and model outputs.
2. **Agreement:** Cohen's κ (three classes) on blind labels.
   - Run the 24-item pilot first.
   - If κ < 0.6, revise the rubric (bump its version) before scaling.
3. **Adjudication:** a third reviewer, or a recorded discussion, settles
   disagreements. Items that stay unresolved remain ambiguous: excluded
   from training and kept as an evaluation slice.
4. **Rubric changes:** a change re-reviews only the items its rule
   touches. Every label keeps `{reviewer, date, rubric_version}`.

### Partitions

**Chronological:**

`train < validation < calibration < test`

- **Embargo:** at least 30 calendar days between consecutive partitions,
  longer than the 20-session feature window, so overlapping windows of one
  company cannot straddle a boundary.
- **Duplicates:** identical cards must share a partition.
- **Synthetic cards:** never in calibration or test. They are kept as a
  separate diagnostic set.
- **Explored days** (2026-09-28/29, 10-02): never in test.
- **Code check:** `laya_ft_pilot.check_partitions` enforces all of the
  above (tested).

## 5. Evaluation, defined before any training

**Test set:** the untouched test sessions with human `final_label`s. All
methods use the same days, the same cards and CPU inference in the
isolated worker.

| Measure | Definition |
|---|---|
| Coverage@10 (primary) | Per day, \|top-10 ∩ review-worthy\| / min(10, #review-worthy), averaged over days |
| Missed review-worthy | Review-worthy cards outside the top 10 when free slots held non-review-worthy cards |
| Precision@10 | Share of each top 10 that is review-worthy |
| AUROC | Review-worthy vs ordinary (ambiguous excluded), with slices by direction and by volatility tercile |
| Ambiguous slice | Score distribution only |
| Calibration | Only if a probability threshold is ever proposed: ECE and reliability fitted on calibration, confirmed on test. With two options, temperature scaling cannot change the ranking, so it does not affect top-10 selection. |
| Runtime | Per-day batch CPU wall time and cold start in the same worker. The original costs about 35 s per 51-card day plus about 24–32 s cold start; the candidate has the same architecture. |

**Comparators:**
1. the original checkpoint with the production question;
2. the rubric reference rules as a ranking (strong count, then moderate
   families, seeded tie-break);
3. the biggest |5d| move;
4. random selection with seeds 0–19.

**Decision logic:** margins are to be fixed in a frozen plan before
training.
- **Candidate ≈ the rules:** training distilled the formula; use the
  formula, not Laya.
- **Laya earns a place** only if it beats the rules, especially on items
  where the human final label differs from the rules.

## 6. Training readiness against the pinned package

**Sources inspected** (no code from them was run except the tokenizer
check below):

| Source | Pin |
|---|---|
| Installed `laya` 0.3.23 README and source | PyPI 0.3.23, the version in `requirements-laya.txt` and image `hermes-laya-worker:2` |
| `NandhaKishorM/laya` tag `v0.3.23` | commit `d8a2e59781ca135169a36095056132e273cd9938` |
| └ `notebooks/laya_finetune_typed_decisions_mps.py` | SHA-256 `22ffb2be…97dc5` |
| └ `notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb` | SHA-256 `d97004a9…eccb0` |
| └ `docs/evals.md` | dataset format for `laya.evals` |
| `main` at `dc721331` (2026-10-05) | Notebook byte-identical. The script differs only in clamping fitted temperatures to the runtime's 0.5–5.0 instead of 0.1–10. |
| Dataset `LocalLLaMA/typed-decisions` | `d0e2f0c4`, row schema only |
| Checkpoint `convaiinnovations/laya` | `55cf4c4e` (our pin; Hub `main` has since moved to `7b928d82`, not adopted) |

**Supported method:** RLCD, as in the official notebook and script, with
`laya.common.build_model / build_sequence / proper_reward / render_options`.
- **Objective:** a Gaussian-noise policy gradient with a proper-scoring-rule
  reward plus cross-entropy.
- **What is trained:** all weights.
  - encoder learning rate 2.5e-5, head 1e-4;
  - AdamW with weight decay 0.01, cosine schedule, gradient clipping 1.0;
  - 4 epochs;
  - gradient checkpointing;
  - training in fp32, exported as fp16 safetensors.
- **Calibration:** the script then fits one temperature per question type.
- **Head-only training** on a frozen encoder is **not** an upstream option.
  A third-party project does it; ours would be a custom loop, so it is not
  assumed.

**Compatibility, verified offline:**
- **Imports:** every training-script import exists in the pinned 0.3.23
  (`QTYPES`, `build_model`, `build_sequence`, `proper_reward`,
  `render_options`, `laya.agent._fix_tokenizer_config`).
- **Our record format:** the converter emits the typed-decisions row
  layout: `state`, `questions` and `gold` as JSON strings, and a noul
  `gold` with `probabilities` over false/true.
- **Tokenizer check:** the 16 trainable pilot rows were passed through the
  pinned tag's own `build_training_item`, using only the pinned tokenizer
  (no weights, no inference). All 16 gave valid 2-option noul items with
  the expected targets; sequences are 195–205 tokens.
- **Evaluation format:** the `laya.evals` export passes the pinned
  `laya-evals validate` (16 examples, 1 question).

**Deviations a future training run must handle:**
- **Hard-coded online dataset.** The script downloads
  `LocalLLaMA/typed-decisions` with `datasets`, which is not in our pins.
  Pre-build its `--items` file from our rows with the same
  `build_training_item` and tokenizer.
- **Context lengths.** `train()` sets `max_len` 1024 / `head_max_len` 256
  and writes them into the exported config, while our checkpoint and
  production use 512 / 192. Pin them back to 512 / 192 for a like-for-like
  candidate.
  - The 48-token option cap is hard-coded in `build_head`, so the
    production "true" criterion (51 tokens) stays cut either way.
- **Calibration slice.** The script fits temperatures on a random 10% of
  the training items, not on our chronological calibration partition. Use
  our partition, or skip calibration while only ranking is used.
- **Upgrades and publishing.** The notebook runs `pip install -U
  "laya>=0.1.6"` and has a push-to-Hub cell. Use `laya==0.3.23` from
  `requirements-laya.txt` and never push.
- **Model name.** The exported config names the model
  `laya-typed-decisions`; rename it for a candidate.

**Hardware, runtime and cost:** none of these figures was measured here.

| Item | Figure | Source / uncertainty |
|---|---|---|
| Model | 421M parameters (ModernBERT-large encoder plus a 2-layer head) | Notebook header; matches the 843 MB fp16 weights |
| Full fine-tune memory | About 7 GB for weights, gradients and AdamW states in fp32, plus activations | Our arithmetic; activations unmeasured |
| Upstream hardware | Kaggle 2× T4 16 GB (DDP, micro-batch 8 per GPU), or a 16 GB Apple-silicon Mac with micro-batch 1 and grad-accum 32 | Upstream README and notebook; not tried here. The script also accepts `--device cpu`, but its CPU runtime is unknown. |
| Upstream runtime | README: "roughly 4–5 hours for 4 epochs over ~30k questions"; notebook cell: "~4 to 6 minutes total" | Upstream figures disagree. Naive scaling to ~1,000 short items × 4 epochs suggests minutes to about 1 h on 2× T4. Unverified. |
| Cost | Not priced | Kaggle's free GPU quota would mean uploading the data to a third party (a decision, §8). A rented GPU needs a quote. |

### Versioning, reproducibility and rollback (design)

**Where a candidate lives:**
- its own directory, never inside the Hugging Face cache of the pinned
  checkpoint;
- a `CANDIDATE.json` that records:
  - base revision `55cf4c4e`;
  - script tag and SHA plus our patch diff;
  - data-manifest and label-review SHA;
  - rubric version, seeds and package versions;
  - fitted temperatures and the weight SHA-256.

**Reproducibility:**
- **Retraining:** seeds are fixed, but GPU/DDP training is not
  bit-reproducible, so the frozen artifact is the weight hash.
- **Evaluation:** deterministic CPU inference in the worker.

### How a candidate would enter the worker (design; nothing built)

1. **A separate pin entry.** Add a candidate ID → weight SHA-256 entry
   beside the original's (`LAYA_SHA256`). `ScorerMetadata.checkpoint` names
   the candidate, so handshakes, snapshots and artifacts can never be
   mistaken for the original.
   - `snapshot_revision()` returns nothing for a non-Hub directory, so the
     manifest check replaces the revision check.
2. **Same image and isolation, read-only mount.** Use the same image
   (`laya` unchanged) and the same `--network none` / read-only /
   cap-drop flags. The candidate directory is mounted read-only, and only
   an offline evaluation runner (like `laya_framing.py`) uses it.
3. **Rollback.** The production default stays the original. Rollback means
   not passing the candidate, and the original's files are never modified.

## 7. Scoped audit (preparation level)

| Area | Result |
|---|---|
| Label provenance | Every record carries `label_status: ai_proposed_unreviewed`, the provenance text, the rubric version and the rules' label beside the proposal. The one AI override is recorded with its reason. The converters refuse non-`human_reviewed` labels unless a format check asks, and always refuse `ambiguous` (tested). |
| Leakage | Labels use only the seven fields. Sampling never reads scores or values (tested). Synthetic and explored days are kept out of test, and identical cards and the embargo are checked (tested). |
| Privacy | Real values, session dates, row IDs and group keys exist only in git-ignored `.laya_ft_pilot/`. The reviewer sheet has no source, date or identity. Committed files contain synthetic cards and aggregate counts only. Model-facing and reviewer-facing text is checked against identity, score and outcome terms (tested). |
| Compatibility | As in §6. Pinned package and tag inspected; tokenizer and evals checks pass; deviations listed rather than patched. |
| Fixed during preparation | The first rule draft counted MACD and correlated location fields as independent signals. On the real sample this promoted a −6.4% move with a saturated MACD to review-worthy. Families and "MACD supports only" were added before the final build. |

## 8. Open decisions (for the project owner)

1. **Reviewers.** Who are the two independent reviewers (and the
   adjudicator)? Nothing scales until the 24-item pilot is reviewed.
2. **Direction.** Should down-only activity stay review-worthy, as it is
   now (direction-neutral, as in production), or be deprioritised for a
   long-only agent? `direction` is recorded either way.
3. **Hardware.** Local machine, Kaggle free GPUs (uploads card data to a
   third party), or a rented GPU, once training is ever approved.
