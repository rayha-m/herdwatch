# Risk card: Cow02

**BRD risk score 0.94** (rank 1 of 10; 0.5 = herd-typical) · data reliability: **good** (39 h observed across both days, mean label confidence 0.93)

**Why she is flagged (behaviour observed by the cameras, compared with her 9 pen-mates):**

- time at the feed bunk lower than herd (117 vs median 288 min/obs-day)
- response to fresh feed (bunk attendance in the 30 min after delivery) lower than herd (22 vs median 51 % of minutes)
- number of bunk visits lower than herd (16 vs median 31 visits/obs-day)

![features vs herd](Cow02_features.png)

**48-hour activity** (purple = lying, blue = feeding, yellow = standing, aqua = drinking, grey = not visible):

![actogram](Cow02_actogram.png)

**Visible clinical signs (agent check on best close-up, estimated):** **not run**: the agent API was unavailable (account credit exhausted) during the final run. Re-run `python -m pipeline.evidence --run full --top 3` to add it.

**How to find her in the pen** (her marks at the time of each clip, from the agent's roll call within 1 h; IDs are reliable for about an hour, see README > Validation): yellow ear tag; no white face; black, dried manure on rump/hindquarters, medium size (agent roll call Sat 07:00); ear tag not visible; no white face; black, rear/side view at water trough, head turned away; medium-large (agent roll call Sat 16:30)

**Video evidence:**

- [Cow02_1_Cam03_1107_072701.mp4](../../evidence_clips/Cow02_1_Cam03_1107_072701.mp4): after a feed delivery (Sat 07:16) the pipeline labels 71% of visible pen-mates as feeding (mostly seen on the bunk cameras, Cam 02/01) while Cow02 is standing (wide Cam 03 view). Cam 03, Sat 07 Nov 07:27:01
- [Cow02_2_Cam02_1107_160817.mp4](../../evidence_clips/Cow02_2_Cam02_1107_160817.mp4): best close-up (used for the clinical-sign check). Cam 02, Sat 07 Nov 16:08:17

**Suggested pen-rider action:** pull and examine (rectal temperature ≥ 40.0 °C / 104 °F, respiratory rate and character, nasal and ocular discharge, ear carriage, lung auscultation). Compare with a herd-mate.

_What this card is and is not:_ behaviour is **observed** (YOLO + tracker + agent on hard cases). Identity and minute labels carry error (see README, Validation). The risk score is **estimated** relative to this herd over 2 days. No clinical records were available, so it is a screening flag, not a diagnosis.