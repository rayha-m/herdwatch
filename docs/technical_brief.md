# Agentic vision for Angus behaviour and BRD risk: technical brief (draft)

**HerdWatch** · Merck–TAMU Animal Science Hackathon 2026 · Pen 6: 10 Angus cows, 3 cameras, 7–8 Nov 2026

## 1. Approach

**The data.** We had 144 camera-hours of 4000×3000 HEVC video (non-standard `GHEC` FourCC) from three fisheye cameras:

- **Cam 01** looks back down the pen from the bunk.
- **Cam 02** looks down onto the feed bunk.
- **Cam 03** sees the whole pen from the back corner.

The cows are near-identical black Angus, ear-tag numbers are rarely legible, and night video is infrared and partly fogged. We streamed all 1,705 available five-minute files straight from the Drive ZIP parts (CRC-checked) and decoded every keyframe, one every 2 s.

**Bulk perception, no LLM.** YOLO11s (COCO, all animal classes merged), a Pen 6 mask and an IoU tracker yield about 1M in-pen detections. Each gets a posture probability (ResNet-18 crop embedding + box geometry, logistic regression on 294 hand labels; **95.9% grouped-CV**, 94.2% at night), bunk and waterer overlap, motion, tag colours and white markings. Identity uses Cam 03 tracklets in 10 slots, assigned by Hungarian matching on appearance, colour, motion plausibility and exclusivity. Cam 01 is mapped onto Cam 03 by a foot-point mapping refined with ICP/RANSAC (**19.6 px** median residual), and Cam 02 onto Cam 01 by position along the bunk. Rules (posture × zone × motion) are fused across cameras into one label per cow-minute, keeping provenance: observed, estimated, agent or not observed.

**Agent for hard cases only.** A Claude Opus 5.5 vision agent receives only the cases the bulk pipeline cannot settle: low-margin identities, ambiguous minutes, drinking candidates and night frames. It plans what would settle the question, then uses tools: zoom to full resolution, step back and forth in time, switch camera, and an 8-s/30-fps chew-rhythm test. It must submit a `self_check`, and the pipeline then tests the answer against geometry (e.g. "feeding" while not at the bunk) and sends contradictions back for another look. Calls are capped and logged. The same agent ran 43 **roll calls**, describing every visible cow's tags and markings every 30 minutes in daylight; we use them to *measure* identity drift.

**Risk.** Per cow-day features come from the BRD behaviour literature:

- time at the bunk and number of visits (Quimby 2001; Sowell 1999; Wolfger 2015);
- response to fresh feed (bunk attendance in the 30 min after a delivery inferred from herd behaviour);
- lying share and longest bout;
- distance to the nearest herd-mate;
- Day 2 vs Day 1 change.

Each feature becomes a robust z-score against the cow's 9 pen-mates. Fixed literature weights combine them into a 0–1 logistic risk with plain-language reasons. **Live mode** re-runs the pipeline on each new clip, updates risk from that prior, and alerts only with evidence in the clip, attaching a 12-s video.

## 2. Results

| | |
|---|---|
| Cow-minutes observed | **21,551 / 28,800 (75%)**. All 2,880 minutes × 10 cows are in `activity.csv` (5,457 bouts) |
| Behaviour, blind audit D (n=51) | per-minute accuracy **61%** (prevalence-weighted); **lying vs not 90%**; day 61%, night 44% |
| Feeding / drinking precision after the last fix (audit E) | 55% / 50% |
| Herd time budget (median cow) | lying 47%, standing 24%, feeding 20%, drinking 2% |
| Identity half-life (agent roll calls) | same-ID attribute agreement **0.45 at ≤1 h** vs 0.14 chance; at chance after about 4 h |
| Agent | 200 calls (about $6); 40 of 60 hard cases answered before the API credit ran out |
| Live alert | 5-min clip processed in **14 s**; 3 evidence-backed alerts with clips; 7 feeding cows stayed quiet |

**How we got there.** Each audit was a fresh random sample of cow-minutes, labelled blind from 3 frames, and each one changed the pipeline:

- **B:** proximity rules over-called social contact (17% precision) and walking (14%).
- **C:** the posture model was 64% accurate at night, because it had almost no IR lying examples.
- **D:** a "head in bunk ⇒ standing" rule turned cows lying near the bunk on Cam 01/03 into "feeding".

We fixed each in turn (night labels, social made agent-only, rule limited to the top-down camera) and re-measured on new minutes.

**BRD screening order.** The top 3 are:

1. **Cow02 (0.94):** 117 vs 288 min/obs-day at the bunk; 22% vs 51% bunk attendance after fresh feed; half the herd's bunk visits.
2. **Cow04 (0.85):** bunk time −70% from Day 1 to Day 2, where the herd fell −36%; weaker response to fresh feed.
3. **Cow03 (0.82):** bunk time −61%; longest lying bout 130 vs 80 min.

Reduced feed intake and weaker bunk response are the earliest BRD signs in feedlot studies, so this is a biologically coherent order. **It is not a diagnosis.** Each risk card links 2–3 clips and describes the animal (tag colour, white face) at the time of each clip, so a pen rider checks the right cow. The live demo independently flagged risk-card cow #2, lying through a feed delivery. Herd-wide, bunk time fell and lying rose from Day 1 to Day 2 in a visibly wet pen; that is why everything is scored relative to pen-mates.

## 3. Limitations: what 10 cows × 2 days can and cannot support

- **No BRD ground truth.** With no health records we cannot report sensitivity, specificity or prevalence; some, all or none of the cows may be sick. The weights are literature-based, not fitted, and the output is a screening order.
- **Identity across days is the weakest link.** The tracker keeps an ID for about an hour (measured above). 48-h per-cow totals therefore partly mix animals. We tried re-anchoring IDs from the roll calls and rejected it: only 14 of 356 descriptions had a legible tag number, and tag colours repeat across cows. We report the drift instead of hiding it.
- **Zone activities without head pose.** Feeding and drinking are about 50–55% precise, because "standing beside" looks like "using". Rumination and grooming need jaw motion: our bulk chew detector did not separate chewing from body sway at these distances, so they come from the agent only and are undercounted.
- **Night.** Cam 02 is IR-blind, and Cam 01 and late-night Cam 03 are fogged.
- **Coverage.** About 23 video files were missing. The agent covered 40 of its planned 60 hard cases, and no clinical-sign checks (ears, nasal discharge) ran on the final cards before the credit ran out. The code adds them on re-run.

## 4. With more data and a ranch deployment we would

1. **Fix identity at the source.** Use RFID at the bunk and waterer, or one tag-facing camera at each. That turns the hardest problem here into a lookup and makes per-cow intake trends trustworthy over weeks.
2. **Label head pose and use clinical outcomes.** A few hundred head-in-trough and muzzle-in-water labels would let a small model replace zone rules. Daily pen-rider scores, rectal temperatures and pull and treatment records would let us fit the risk weights and report lead time against diagnosis, as Quimby et al. did with feeding-behaviour systems.
3. **Spend the agent where it pays.** Run it on every alert for a clinical-sign check (ear carriage, nasal and ocular discharge, breathing at 30 fps) and on daily roll calls. At about $0.03 a call, a pen costs cents a day.
4. **Deploy at the edge.** A Jetson-class box per pen running YOLO + tracking at 0.5 fps, with batch risk updates every hour. Alerts go to the pen rider's phone with the clip, the animal's marks, and a check list: temperature, ears, nose, breathing.

*AI disclosure: Claude Code helped write the code and this brief; the team ran, reviewed and audited every result. Repository and exact commands: README.*
