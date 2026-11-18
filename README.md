# HerdWatch: agentic vision for Angus behaviour and BRD risk (Pen 6)

Team project for the Merck–TAMU Animal Science Hackathon 2026 (*Agentic Vision for Angus Behavior and BRD Prediction*).

This system watches the three Pen 6 cameras over both days. It labels what each of the 10 cows is doing every minute and ranks the cows by BRD risk, with reasons a veterinarian can check and video clips as evidence. It also runs as a live early-warning loop on new video. A fast detector and tracker do the bulk of the work. A Claude vision agent is called only for cases they cannot settle, and it checks its own answers. **Every accuracy number below comes from blind audits we ran on the event video**, including the parts that did not work well.

**Demo (84 s):**

https://github.com/user-attachments/assets/ccad8ccb-60dd-4424-8fda-f045c0a51933

Two-page technical brief: [`docs/technical_brief.md`](docs/technical_brief.md) · Validation numbers: [`outputs/validation.json`](outputs/validation.json)


## Contents
[Approach](#approach) · [Results](#results) · [Reproduce](#reproduce-exact-commands) · [Outputs](#outputs) · [Observed vs estimated vs assumed](#observed-vs-estimated-vs-assumed) · [Limitations](#limitations-and-what-2-days--10-cows-can-support) · [Models, data and AI disclosure](#models-data-tools-and-ai-disclosure)

## Approach

```
Drive ZIP parts ──zipstore (stream, CRC-checked, no extraction)──► ffmpeg HEVC keyframes (0.5 fps, 1280×960)
 │
 ├─ Bulk (no LLM)  YOLO11s cattle detection → Pen 6 mask → IoU tracker → per detection:
 │                 posture P(standing), bunk/waterer overlap, motion, tag colours, ResNet-18 embedding
 ├─ Identity       Cam 03 (whole pen) tracklets → 10 slots (appearance + colour + motion + exclusivity)
 │                 Cam 01→Cam 03 foot-point mapping (landmarks + ICP/RANSAC); Cam 02→Cam 01 position along the bunk
 ├─ Behaviour      per 2-s sample (posture × zone × motion) → camera-aware fusion → 1 label per cow-minute → bouts
 ├─ Agent          Claude Opus 5.5 on HARD cases only, capped: low-margin identities, ambiguous minutes, night.
 │                 Tools: zoom · look back/forward in time · other camera · chew-rhythm test. Must self-verify;
 │                 a geometry self-check bounces contradictions back once. Also: 43 identity "roll calls".
 ├─ Level 2        per cow-day features → herd-relative robust z → BRD risk 0–1, rank, reasons, dashboard,
 │                 risk cards with clips + "how to find her" marks
 └─ Level 3        new clip → same pipeline → prototype IDs → risk update → evidence-gated alert + clip (+ agent check)
```

**Why this split.** 144 camera-hours sampled at 0.5 fps is 260k frames and about 1M cow detections inside the pen. A VLM on every frame is slow, costly and no better on easy frames. So YOLO and rules label the bulk. The agent's budget goes where single frames fail: identity conflicts, deciding whether a cow at the waterer is drinking or just standing there, rumination (which needs motion over time), dusk and IR frames, and visible clinical signs.

**The agent loop.** Each hard case starts with a question and two images. The agent plans which evidence would settle it. It then calls tools (`look` zooms to full resolution, `look_sequence` steps through time, `look_other_camera` switches views, `chew_check` measures jaw rhythm over 8 s at 30 fps) and must `submit` with an explicit `self_check`. The pipeline then tests that answer against geometry: "feeding" while not at the bunk, "lying" while the posture model is >90% sure she stands, rumination without a chew test. On a conflict the agent is told why and must look again. Every case is saved as a trace (`agent_traces/TRACES.md`).

**Cameras.** `Cam 01` (file suffix 006) is front cam 1: under the shade beam, looking back down the pen. `Cam 02` (017) is front cam 2: top-down on the feed bunk. `Cam 03` (003) is the back cam: a wide view of the whole pen. Zones are hand-drawn polygons in `pipeline/config.py`; `python tools/draw_zones.py` renders overlays locally.

## Results

### Level 1: watch the cows
| | |
|---|---|
| Video processed | 1,705 of ~1,728 five-minute files (4 Drive ZIP parts never finished downloading), all 3 cameras, both days |
| Cow-minutes observed | **21,551 / 28,800 (74.8%)**. The rest are `other_not_visible` (out of view, IR-blind cameras at night, missing files) |
| `activity.csv` | 5,457 bouts. Every cow has exactly 2,880 minutes |
| Herd time budget (median cow, % of observed) | lying 47.4 · standing 23.6 · feeding 20.2 · drinking 2.0 · walking 0.2 |
| ID-check frames | 288 (every 30 min × 3 cameras × 48 h; slots with no cattle or no video are marked as such) |

**Posture model** (lying vs standing; ResNet-18 + box geometry, logistic regression, 294 hand labels, grouped 5-fold CV): **95.9%**. By day 96.8% (n=190); night IR 94.2% (n=104).

**Behaviour accuracy: blind audits.** Each audit is a fresh stratified random sample of about 60 cow-minutes, labelled from 3 frames without seeing the pipeline's label. Each round found a failure, we fixed it, then re-measured on new minutes:

| Audit | Pipeline version | Per-minute accuracy (prevalence-weighted) | Lying vs not | Main finding |
|---|---|---|---|---|
| B (n=52) | first rules | 75.8% | 86.5% | drinking / social / walking over-called (precision 30 / 17 / 14%) |
| C (n=49) | tighter activity rules | 50.1% | 81.6% | night IR: lying cows called standing (old posture model **64%** on night crops); proximity "social" 0/8 |
| D (n=51) | night labels + social made agent-only | **60.6%** (day 60.7%, night 43.5%) | **90.2%** | "head in bunk ⇒ standing" rule turned lying cows on Cam 01/03 into feeding (feeding precision 11%) |
| E (n=17) | rule limited to Cam 02 | precision only: feeding **55%**, drinking **50%** | | remaining errors: cow *beside* the bunk or waterer, head not resolvable |

The audits are small (±~13 points), and B and D sampled different label mixes, so read the trend rather than the third digit. Posture is reliable. Zone-based activities are not, without head pose. That is the agent's job, and it was cut short (see Agent).

**Identity across cameras and days.**

- *Cross-camera.* The Cam 01→Cam 03 mapping has a median residual of **19.6 px** on 2,820 inlier pairs; 2,027 of 6,291 Cam 01 tracklets carry a Cam 03 ID. Cam 02→Cam 01 (slope 0.86) links 507 of 1,893 Cam 02 tracklets.
- *Over time (honest result).* A manual audit of Cam 03 crops spread over 48 h found **22 of 42 (52%)** consistent with their ID's majority animal. The agent's 43 roll calls (every 30 min in daylight, describing each cow's tag colour and white markings) measure the tracker's **identity half-life**: same-ID observations agree **0.45 at ≤1 h** against **0.14 chance**, and fall to chance (0.25–0.32) beyond about 4 h (`id_consistency_rollcall.png`).
- We tried re-anchoring IDs from the roll calls (`BV_REANCHOR=1`). Tag numbers were legible in only 14 of 356 observations, and colours repeat across cows, so the 10-cow register was too noisy to trust. It is **off** by default.

**Agent.** 200 Claude calls (about $6.30). 60 hard cases were selected and **40 answered** (18 identity, 11 day behaviour, 11 night). The Anthropic account ran out of credit mid-run, and every unanswered case kept its YOLO/rule label. There were also 43 roll calls. Traces chosen by `tools/render_traces.py` are in [`TRACES.md`](outputs/level1/full/agent_traces/TRACES.md):

- **success** `full_bh_Cow03_1108_1202`
- **failure** `full_id_Event20261108134131003_4`
- **night** `full_bh_Cow02_1107_0222`

### Level 2: BRD risk
All ten cows have at least 27 observed hours ("good" reliability). Top 3 ([`brd_risk.csv`](outputs/level2/brd_risk.csv), [risk cards](outputs/level2/risk_cards/)):

| Rank | Cow | Risk | Top reasons (vs her 9 pen-mates) |
|---|---|---|---|
| 1 | Cow02 | 0.94 | bunk time 117 vs median 288 min/obs-day; fresh-feed response 22 vs 51%; 16 vs 31 bunk visits/obs-day |
| 2 | Cow04 | 0.85 | bunk time −70% Day 2 vs Day 1 (herd −36%); fresh-feed response 39 vs 51%; bunk time 220 vs 288 |
| 3 | Cow03 | 0.82 | bunk time −61% Day 2 vs Day 1; longest lying bout 130 vs 80 min; bunk time 225 vs 288 |

**Herd context, observed:** from Day 1 to Day 2 the whole herd's median bunk time fell from 361 to 203 min/obs-day, and lying rose from 40% to 55% of observed time. The pen was visibly wet on Day 2. Because of this pen-wide shift, every feature is scored relative to pen-mates rather than against absolute thresholds.

**Visualisation:** [`dashboard.html`](outputs/level2/dashboard.html) is a self-contained HTML page with the ranking, per-feature strip plots, a 48-h actogram for every cow, herd feeding with the inferred feed deliveries, and a table view.

### Level 3: early warning
`python -m pipeline.live --clip <new.avi>` processes a 5-minute clip in about **14 s** on an M3 Pro. On Sun 09:46 (Cam 03) it raised 3 alerts, each with a 12-s clip ([`outputs/level3/alerts`](outputs/level3/alerts)):

- two cows lying for most of the clip while at least 60% of pen-mates were at fresh feed;
- one cow standing away from the bunk throughout.

The other 7 cows were feeding and stayed quiet. An alert needs evidence *in the clip*, and there are at most 3 per clip. Live IDs come from appearance prototypes. On this clip they matched the batch IDs 33% of the time, but as a clean one-to-one renaming. The live "Cow03" is batch **Cow04**, risk card #2: the same animal flagged independently by both levels.

## Reproduce (exact commands)

Tested on macOS (Apple M3 Pro, 18 GB, MPS), Python 3.9.6, ffmpeg 8 (Homebrew).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && brew install ffmpeg
mkdir -p work/weights && curl -L -o work/weights/yolo11s.pt https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s.pt
echo "ANTHROPIC_API_KEY=<your key>" > .env                 # git-ignored

.venv/bin/python -m pipeline.zipstore --src ~/Downloads/hackathon   # index AVIs inside the Drive ZIP parts
.venv/bin/python -m pipeline.index_videos
.venv/bin/python -m pipeline.detect_track --all                     # ~1.5 h with 4 workers (--cam X [--reverse])
.venv/bin/python -m pipeline.posture train                          # data/labels/posture_labels.csv
BV_MAX_AGENT_CALLS=400 .venv/bin/python -m pipeline.level1 --cams "Cam 01" "Cam 02" "Cam 03" --name full --max-cases 60
#   re-run later without new API calls:  ... --name full --reuse-agent
.venv/bin/python tools/fill_idcheck.py --run full
.venv/bin/python -m pipeline.idcheck --run full                     # roll calls (cached) + identity curve
.venv/bin/python tools/render_traces.py --run full
.venv/bin/python -m pipeline.level2 --run full
.venv/bin/python -m pipeline.evidence --run full --top 3            # clips, risk cards, agent clinical check
.venv/bin/python -m pipeline.dashboard --run full
.venv/bin/python -m pipeline.live --clip /path/to/EventYYYYMMDDHHMMSS003.Avi --run full   # or --watch <folder>
.venv/bin/python -m pipeline.validate behaviour-sheets --run full   # blind audit sheets; score with `validate score`
```

Development slice (one hour, one camera): `python -m pipeline.level1 --cams "Cam 03" --start "2026-11-08 17:00" --hours 1 --name slice_c3_1h --max-cases 8`.
`BV_MAX_AGENT_CALLS` caps all Claude calls (default 400). Every call is logged to `work/agent_usage.jsonl`. When the cap is reached or the API is unavailable, the pipeline keeps the YOLO/rule label and says so.

## Outputs

| Requirement | File(s) |
|---|---|
| activity.csv (bouts, every cow, every minute, both days) | `outputs/level1/full/activity.csv`; per-minute table `activity_minutes.csv` (with source, confidence, cameras) |
| ID-check frames every 30 min on each camera | `outputs/level1/full/id_check/` (288) |
| Per-cow time budgets | `outputs/level1/full/time_budget_minutes.csv`, `time_budget_pct_of_observed.csv` |
| Agent traces (success / failure / night) | `outputs/level1/full/agent_traces/TRACES.md` (+ JSON per case, `agent_cases.csv`) |
| brd_risk.csv + visualisation | `outputs/level2/brd_risk.csv`, `features_per_cow_day.csv`, `risk_contributions.csv`, `dashboard.html` |
| Risk cards (top 3) with 2–3 clips | `outputs/level2/risk_cards/CowXX.md`, `outputs/evidence_clips/*.mp4` |
| Live alert | `outputs/level3/alerts/*.md / .json / .mp4`, `outputs/level3/live_state.json` |
| Validation | `outputs/validation.json`, `outputs/posture_model_cv.json`, `outputs/level1/full/id_consistency_rollcall.*`, `crosscam_report.json` |

`activity.csv` columns: `cow_id, behavior, start, end, duration_min, source, mean_confidence, cameras, night`. `source` is one of:

- `observed`: seen by YOLO + rules;
- `estimated`: inferred, e.g. drinking from presence at the waterer;
- `agent`: decided by the Claude agent;
- `not_observed`: no camera saw the cow that minute, so the label is `other_not_visible`.

## Observed vs estimated vs assumed

| | |
|---|---|
| **Observed** | Cow detections and positions; lying/standing; presence in the bunk/waterer zone; movement between 2-s samples; minutes when a cow is not visible; the herd-wide Day 2 changes |
| **Estimated** | Identity (reliable for about 1 h, see above); feeding (head in the bunk zone; reliable on Cam 02); drinking (at the waterer, `source=estimated`); rumination, grooming and social contact (agent only); every BRD risk score |
| **Agent-decided** | 40 hard cases in `agent_cases.csv`, each with a trace |
| **Assumed** | The yellow tank is the waterer (the pen map shows none). Camera clocks agree within about 1 s. Feed deliveries are inferred from herd behaviour (there is no feed log; 5 daytime deliveries look plausible, and 2 night-time ones at 01:41 and 20:34 are suspect). Risk weights come from the literature, not fitted (there are no health records). |

## Limitations and what 2 days × 10 cows can support

- **No ground truth for BRD.** 10 animals, 2 days and no clinical records cannot estimate sensitivity, specificity or prevalence. The ranking is a *screening order*: it flags cows whose behaviour departs from their pen-mates and from their own Day 1, with video for a vet to check. It is not a diagnosis. Some, all or none of the cows may have BRD.
- **Identity over days is the weakest link.** The tracker's IDs hold for about an hour. 48-h per-cow aggregates (time budgets, Level 2 features) therefore partly mix animals, as the high-feeding, low-lying profiles of Cow05 and Cow10 may show. Risk cards counter this by describing the animal in each clip (tag colour, white face) and timestamping the evidence, so a pen rider checks the right cow. Robust 48-h identity needs tag-facing cameras at the bunk or waterer, or RFID.
- **Zone activities without head pose.** Feeding and drinking precision is about 50–55%. A cow standing beside the bunk or waterer looks like one using it.
- **Night.** Cam 02 IR is too dark for detection, Cam 01 IR is fogged, and Cam 03 fogs late at night. Night accuracy is lower (43.5% in audit D).
- **Rumination and grooming.** A bulk chew detector (8-s motion spectrum) did not separate chewing from body sway at these distances: lying cows showed no consistent 0.7–1.4 Hz peak. So these behaviours are labelled only by the agent, and time budgets undercount them.
- **Agent coverage.** API credit ran out after 40 of 60 hard cases. No clinical-sign checks were made for the final risk cards or live alerts; the cards say so, and the code adds them when re-run with credit.
- **Sampling.** At 0.5 fps, bouts shorter than about 10 s can be missed. About 23 five-minute files were missing from the download.

## Models, data, tools and AI disclosure

| What | Version | Use |
|---|---|---|
| Ultralytics YOLO11s, COCO-pretrained | ultralytics 8.4.213; `yolo11s.pt` (v8.3.0 assets) | cattle detection (COCO animal classes merged: the model calls black Angus cow, bear, sheep or horse) |
| torchvision ResNet-18 (ImageNet) | torch 2.8.0 / torchvision 0.23.0 | crop embeddings for posture and identity |
| scikit-learn logistic regression / RANSAC | 1.6.1 | posture classifier; cross-camera mappings |
| Claude Opus 5.5 (`claude-opus-5-5`) | Anthropic API, `anthropic` SDK 0.125.0 | hard-case vision agent, roll calls, clinical checks |
| ffmpeg | 8.x | HEVC decoding (the AVIs carry a non-standard `GHEC` FourCC; we force `-c:v hevc`), clips |

- **Labels.** 294 posture labels and 4 blind behaviour audit sets (B–E) were drawn by the team from event frames (`data/labels/`), as the brief allows. No outside datasets were used.
- **AI tools.** Claude Code (Anthropic) helped write this code and documentation. The team ran and reviewed everything and is responsible for the claims.
- **Literature behind the risk features:**
  - Quimby et al. 2001: feeding behaviour flags BRD about 4 days before pen riders.
  - Sowell et al. 1999: morbid calves spend less time at the bunk.
  - Wolfger et al. 2015: feeding and drinking behaviour before BRD diagnosis.
  - Toaff-Rosenstein et al. 2016: sickness behaviour after an experimental respiratory challenge.
  - Hart 1988: sickness behaviour.
- **Event data.** Raw video and the agent's working images are git-ignored. Only ID-check frames, short evidence clips and derived tables are committed.
