# Laya review-priority fine-tuning: pilot scope and readiness (v0, 2026-10-05; rubric v0.2, 2026-10-06)

**Status: preparation only. The rubric and pilot have been revised; human
review and dataset expansion are pending. Training is not approved.**

The pilot is internally consistent, but that does not make fine-tuning
ready.

Nothing was trained, downloaded, provisioned, scored or deployed for this
document. The production selector, question, worker, card features, rules
and watchlists are unchanged.

**Scope:**
- **Task:** a narrow review-priority classifier for the existing Laya
  selector.
- **Not in scope:** returns, trade quality or buy signals.

**Built offline:**
- the proposed rubric, now v0.2 (§2);
- a 24-example private pilot with its versioned v0.2 revision and blind
  review sheets (§3);
- a dataset and evaluation design (§4–5);
- a training-readiness map against the pinned package (§6);
- one helper module, `backtest-harness/laya_ft_pilot.py`, with 25 offline
  tests.

**Evidence base:**
- `laya_vs_controls_2026-10-04.md`: Laya is not shown to differ from
  random selection.
- `laya_framing_2026-10-04.md`: no framing or label variant helps, and
  there is label bias.
- The pinned package README calls the base checkpoints "a fast base to
  specialise, not a zero-shot decision engine".

## 1. Task definition (settled 2026-10-06)

**Question:** does this company-day show unusual technical activity that
warrants further review? The answer is judged from the seven numeric card
fields only.

**What it is:** the meaning of the production question
(`laya_adapter.REVIEW_QUESTION`, `noul-criteria-v2`), which is unchanged.

**What it is not:**
- whether to buy;
- whether a long position will profit;
- whether all indicators agree;
- whether the company will pass downstream risk checks.

**Direction and agreement:**
- Downward activity can warrant review, exactly like upward activity.
- Conflicting indicators can also warrant review.
- Neither makes a card ambiguous. Each example records a `direction`
  (up / down / mixed / none) for information only.

**Three annotation states:**
- **review-worthy;**
- **ordinary / lower priority;**
- **ambiguous / insufficient evidence:** the card cannot support a
  dependable label under the rubric.

Ambiguous is an annotation state, never a production output and never a
training label.

**What the seven fields cannot support** (documented, not fixed; no
production feature was changed and no data source was added):

| Limitation | Consequence for labels |
|---|---|
| No market or sector context | Labels mean "unusual on this stock's own numbers", not "unusual vs. the market". |
| No longer-run history | Only 20-day volatility exists. "Unusual for this particular stock" cannot be judged and must not be assumed. |
| No price levels, longer trend, gaps or intraday data | A good long setup cannot be labelled, and labels must not claim one. |
| No liquidity, session-length or data-quality field | A very low-volume day may be a shortened session or a data gap. |
| MACD direction saturates | \|MACD dir\| ≥ 0.9 on 54 of 153 explored cards (35%). It supports other signals only and never counts. |
| Volatility includes the recent move | The vol-scaled move is partly circular. |
| SMA distance and range position are correlated | They count as one "location" family. |

**Labels are independent of:**
- future prices and outcomes;
- company identity and dates;
- Laya scores;
- Hermes decisions;
- prior shortlists.

The tools never read the score fields of the source file. The sample key
uses only session and row ID (tested).

## 2. Labelling rubric `review-rubric-v0.2-proposed`

**Status:** proposed until human review.

**Implementation:** `laya_ft_pilot.rubric_reference`.

**It is a fixed formula, disclosed as such.** Every label follows
deterministically from the anchors below, with no judgement overrides.

**What follows from that:**
- If human reviewers simply apply these rules, a fine-tuned Laya would
  distil this formula, not show better judgement. The formula itself would
  then be the cheaper selector.
- No subjective exceptions were added to make training look necessary.
- Whether training could add anything depends on humans departing from
  these rules for reasons the card supports.

### Fields, units and what the rubric sees

The rubric judges the exact 4-decimal values the model receives. The
reviewer rendering shows the same values with units (tested):
fractions as percent with 2 decimals; RSI, MACD and relative volume with 4
decimals.

| Field | Unit / meaning | Limitation |
|---|---|---|
| 5d return | close vs the close 5 sessions earlier | — |
| vs 20d avg | close vs its 20-day simple average | correlated with range position |
| RSI14 | 0–100 (Wilder); 50 = balanced | — |
| MACD dir | tanh(MACD(12,26,9) histogram / price / 0.005), −1…+1 | saturates; supporting only |
| rel vol | today's volume ÷ mean of the prior 20 sessions | cannot flag short sessions |
| ann vol | stdev of the last 20 daily log returns × √252 | 20 days only; includes the recent move |
| 20d range pos | 0 = 20-day low, 1 = 20-day high | correlated with SMA distance |

**Derived:** 5d move in own-vol sd = 5d return / (ann vol × √(5/252)).

### Anchors

Correlated fields share a family. **Every number is a proposed policy
choice, not an empirically optimal threshold.**

| Family | Strong (inclusive) | Moderate (inclusive) |
|---|---|---|
| Move | \|5d\| ≥ 7%, or own-vol move ≥ 2.5 sd with 4% ≤ \|5d\| < 7% | \|5d\| ≥ 4% |
| Location | \|vs 20d avg\| ≥ 7% | \|vs 20d avg\| ≥ 4%; range pos ≥ 90% or ≤ 10% |
| Oscillator | RSI ≤ 25 or ≥ 75 | RSI ≤ 32 or ≥ 68 |
| Participation | rel vol ≥ 2.5× | rel vol ≥ 1.6× |
| Momentum | supporting only; never counted | \|MACD dir\| ≥ 0.6 is noted |

### Labels

1. **Review-worthy:** any strong signal, or moderate signals in two or
   more families, in any direction.
   - Conflicting directions do not reduce this.
   - **Combinations that strengthen review priority**, for ranking within
     review-worthy, where they are recorded in the rationale:
     - more families with a strong signal;
     - then more counted families.
     - Typical strong combinations: a move with participation (a move on
       heavy volume), or location with an oscillator extreme.
2. **Ordinary / lower priority:** counted signals in one family only, or
   none (MACD alone is none).
3. **Ambiguous / insufficient evidence:** only when no family counts and
   the card shows an unexplained extreme it cannot interpret:
   - **rel vol ≤ 0.40×:** a shortened session, a data gap or genuinely
     thin trading cannot be told apart;
   - **ann vol ≥ 60%:** recent turbulence with quiet current fields; the
     card has no longer-run baseline to say whether this is unusual for the
     stock or already over.

   Each ambiguous record carries this reason (tested).

**Borderline values:**
- Anchors are inclusive cut points on the 4-decimal values.
- A value within 10% of an anchor, measured from the field's neutral
  value (0, RSI 50, rel vol 1.0, range pos 50%), is **flagged**
  `near_anchor` for reviewer attention.
- The label still **follows the anchor**: nearness is not missing
  evidence. Human reviewers may disagree on such cards; that is what the
  review is for.

**Contradictions in v0.1, corrected:**
1. v0.1 called "conflicting directions" ambiguous. That contradicts the
   task, so disagreement no longer decides anything.
2. v0.1 called "near an anchor" ambiguous, which made a lone strong value
   less certain than a weaker one. Nearness is now a flag.
3. v0.1 allowed a recorded AI judgement override, citing an "unusually calm
   stock". The card cannot support that, so overrides are removed.
4. v0.1's "ordinary looks like ±2–3%" table conflicted with the 4%
   anchors. Ordinary is now defined only by the anchors.
5. v0.1 judged full-precision values but rendered rounded ones (RSI to 1
   decimal, volatility to 0). Both now use the model's 4-decimal state.

## 3. Pilot (private, git-ignored)

**Location:** `backtest-harness/.laya_ft_pilot/`. History is preserved
there:

| Folder | Content |
|---|---|
| `20261005T171428Z/` (v0.1, untouched) | original 24 records, labels, provenance, review sheet; `pilot_dataset.json` SHA-256 `a2867fdc…b868` |
| `ai_overrides.json` (v0.1, untouched) | the one recorded v0.1 override |
| `20261006T142253Z_revision-v0.2/` | `pilot_revision.json`, `review_blind.csv`, `proposals.csv` |

**How the revision was made:**
- **Command:** `python backtest-harness/laya_ft_pilot.py revise <parent> --parent-sha256 <hex>`.
- **Refusals:** it refuses unless the parent's hash matches and every card
  value, sample member and private source equals its source. Real cards
  are re-derived from the hash-verified saved run.
- **The parent is only read** (tested; parent hashes re-checked after the
  run).

**Sample, unchanged:**
- **12 real cards:** per session, the 4 with the smallest
  `sha256("laya-ft-pilot-v1|session|row_id")`, 12 different companies,
  from the saved Laya-vs-controls cards (`8d140f99…08fa`). Development
  material only.
- **12 synthetic cards:** `laya_ft_pilot.SYNTHETIC`.

**Each revised record carries:**
- the opaque ID, source type and unchanged card values;
- the rendering;
- `previous_label` with `previous_rubric_version`;
- the revised `proposed_label` and `rubric_version`;
- `direction`, a field-tied `rationale` and `flags`;
- `ambiguity_reason` (ambiguous only);
- `change_reason` (exactly when the label changed);
- `label_status: ai_proposed_unreviewed`;
- provenance: AI-proposed by Claude applying v0.2 exactly; not
  human-reviewed, not ground truth, not approved for training.

### Label counts (aggregate)

| | Review-worthy | Ordinary | Ambiguous |
|---|---|---|---|
| v0.1, real (12) | 4 | 4 | 4 |
| v0.2, real (12) | 5 | 6 | 1 |
| v0.1, synthetic (12) | 5 | 3 | 4 |
| v0.2, synthetic (12) | 6 | 4 | 2 |

**5 of 24 labels changed** (3 real, 2 synthetic):

| Change | Count | v0.1 cause |
|---|---|---|
| ambiguous → review-worthy | 1 | conflicting directions |
| ambiguous → review-worthy | 1 | near anchor |
| ambiguous → ordinary | 2 | near anchor |
| ambiguous → ordinary | 1 | the removed override |

No label moved out of review-worthy or ordinary.

### Synthetic examples (public)

| Situation | Card (4-decimal state) | v0.1 → v0.2 | Why under v0.2 |
|---|---|---|---|
| Pullback after a run-up | 5d −6.00%, vs avg +4.50%, RSI 52, MACD −0.45, vol 1.2×, ann vol 35%, range 62% | ambiguous → **review-worthy (mixed)** | Moderate move plus moderate location = 2 families. The disagreement is itself activity. |
| Lone move near anchor | 5d +6.80%, vs avg +3.00%, RSI 63, MACD +0.40, vol 1.4×, ann vol 30%, range 85% | ambiguous → **ordinary** | One family (move). 6.80% is flagged near the 7% anchor, but the label follows the anchor. |
| Sell-off on volume | 5d −11.0%, vs avg −9.0%, RSI 22, MACD −0.80, vol 3.1×, ann vol 45%, range 3% | review-worthy → review-worthy (down) | Strong in four families. Downward activity counts. |
| High volatility only | 5d +1.0%, vs avg −1.0%, RSI 48, MACD −0.10, vol 1.05×, ann vol 75%, range 45% | ambiguous → ambiguous | No family counts. Turbulence without a baseline is insufficient evidence. |
| Volume spike, flat price | 5d +0.8%, vs avg +0.5%, RSI 53, MACD +0.10, vol 3.4×, ann vol 24%, range 60% | review-worthy → review-worthy | Strong participation alone. |

### Blind review sheets

- **`review_blind.csv`** holds the card and seven values only. It shows no
  proposal, previous label, source, date or identity (tested). For each
  card the reviewer records their label (review_worthy / ordinary /
  ambiguous), a reason, the insufficient-evidence reason if ambiguous,
  their name and the date.
- **`proposals.csv`** is opened only after the blind sheet is complete. It
  shows the v0.2 proposal, rationale, ambiguity reason and near-anchor
  fields. The reviewer records accept / change / ambiguous, the
  `final_label`, their name and the date.
- **Agreement** is computed on the blind labels.

**Safeguards:** the converters still refuse any record that is not
`human_reviewed`, and always refuse ambiguous labels (tested). No status
was changed by this revision.

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

- **Reasoning:** at about 35–40% prevalence, the test set holds about
  100 review-worthy cards, enough to estimate recall to roughly ±6 points.
- **Rubric v0.2 on the 153 explored cards** (aggregate):
  - 59 review-worthy (17–22 per day, more than the 10 shortlist slots);
  - 92 ordinary and 2 ambiguous;
  - 84 (55%) carry at least one near-anchor flag. Many real cards sit
    close to the proposed cut points, so labels are sensitive to those
    policy choices. Human agreement must be measured before scaling.
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
| Label provenance | Every v0.2 record carries `label_status: ai_proposed_unreviewed`, the provenance text, the rubric version, its previous label and version, and a change reason exactly when the label changed. A record claiming any review status fails validation. The converters refuse non-`human_reviewed` labels unless a format check asks, and always refuse `ambiguous` (tested). The v0.1 folder and its override file are unchanged; their hashes were re-checked after the revision. |
| Leakage | Labels use only the seven fields. Sampling never reads scores or values (tested). Synthetic and explored days are kept out of test, and identical cards and the embargo are checked (tested). |
| Privacy | Real values, session dates, row IDs and group keys exist only in git-ignored `.laya_ft_pilot/`. The reviewer sheet has no source, date or identity. Committed files contain synthetic cards and aggregate counts only. Model-facing and reviewer-facing text is checked against identity, score and outcome terms (tested). |
| Compatibility | As in §6. Pinned package and tag inspected; tokenizer and evals checks pass; deviations listed rather than patched. |
| Fixed during preparation (v0.1) | The first rule draft counted MACD and correlated location fields as independent signals. On the real sample this promoted a −6.4% move with a saturated MACD to review-worthy. Families and "MACD supports only" were added before the final build. |
| v0.2 revision audit (2026-10-06) | **Inconsistencies:** the five v0.1 contradictions are listed and corrected in §2. **Unsupported rationales:** v0.2 rationales are generated only from the card's fields; context terms the card cannot carry ("usually", "historically", sector, news…) fail validation (tested); the v0.1 "usually calm stock" override is removed. **Symmetry:** mirrored up/down cards get the same label (tested). **Privacy:** real values stay in the ignored folder; this document has synthetic cards and aggregate counts only. **Scope:** no production feature, question, worker, rule or watchlist changed; no model, Docker, market data or training. |

## 8. Open decisions (for the project owner)

1. **Reviewers.** Who are the two independent reviewers (and the
   adjudicator)? Nothing scales until the 24-item pilot is reviewed.
2. **Direction:** settled 2026-10-06. Downward and conflicting activity
   can warrant review (§1).
3. **Hardware.** Local machine, Kaggle free GPUs (uploads card data to a
   third party), or a rented GPU, once training is ever approved.
