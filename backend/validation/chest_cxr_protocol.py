"""Radiograph-native chest (CXR) search protocol — the Arm-1 "search protocol" lever.

This is the protocol the agent FOLLOWS (written to protocol.md by AgentRunner) for a chest
RADIOGRAPH, in place of the MRI-tuned chest_master.py. It exists because the measured CheXpert
profile is a conservative under-caller (~43-50% sensitivity, pneumothorax 0%): the first read
does not systematically interrogate the zones where subtle findings hide on a 2D projection.

It changes ONE thing vs the baseline: it forces a disciplined, region-by-region radiograph
search with explicit attention to the classic blind spots — WITHOUT lowering the bar for a call
(specificity is protected by a focal-outlier + corroboration/mimic-veto gate and a normal guard).
The OUTPUT contract (summary.json + patient block) is supplied by AgentRunner._build_prompt and
overrides anything here.
"""

CHEST_CXR_PROTOCOL = """CHEST RADIOGRAPH (CXR) SEARCH PROTOCOL — systematic read, anti-hallucination, tier-disciplined.
This is a PLAIN RADIOGRAPH (CR/DX): a 2D projection. Read radiographic density only — alignment,
lucency vs opacity, silhouette signs, lines/edges. DO NOT invent MRI signal (T1/T2/STIR/DWI) or CT
attenuation; they do not exist here. COMPUTE/inspect deliberately; do not glance once and summarize.

STEP 0 — TECHNICAL ADEQUACY (state it, do not skip)
Note projection (PA/AP/lateral), rotation, inspiration (count posterior ribs above the diaphragm),
penetration, and any cut-off field of view. A limited film CAPS the affected call at Tier D
("cannot be reliably assessed due to ...") — never fabricate through a technical limitation.

STEP 1 — NORMAL ANCHOR (protects specificity)
Before hunting, write a one-line baseline impression: on its own merits does this film look NORMAL
or ABNORMAL? Fix that on the record. The systematic hunt below must OVERTURN a stated-normal baseline
only against discrete, corroborated evidence — it must never drift into a finding by accumulating vague
suspicion. A confident clean read on a normal film is a correct, complete result, not a failure.

STEP 2 — SYSTEMATIC ZONE SWEEP (inspect EVERY item; a skipped zone is a failed read).
Use the standard "ABCDE + blind spots" radiograph search. For each, state normal or the specific
abnormality, and verify laterality (patient-right = image-left):
  A. AIRWAY/MEDIASTINUM — trachea midline; mediastinal width/contour; aortic knob; paratracheal stripe.
  B. BREATHING/LUNGS — divide EACH lung into upper / mid / lower zones; compare left vs right for a
     focal density or lucency difference. Then the SUBTLE-FINDING blind spots, explicitly:
       - APICES (behind clavicles): apical cap, small apical pneumothorax, Pancoast opacity.
       - LUNG PERIPHERY / pleural edge: a thin visceral pleural line with NO lung markings beyond it
         = pneumothorax (on a supine/AP film look instead for a deep, lucent costophrenic sulcus =
         "deep sulcus sign"). Small nodule at the periphery — scan the edge, not just the central lung.
       - RETROCARDIAC left lower lobe (behind the heart): look THROUGH the cardiac silhouette for a
         density or an air-bronchogram; loss of the medial left hemidiaphragm = retrocardiac consolidation/collapse.
       - HILA — size, density, position; a unilaterally dense/bulky hilum is abnormal.
  C. CARDIAC — cardiothoracic ratio (cardiomegaly if > 0.5 on a PA film); cardiac borders (silhouette sign).
  D. DIAPHRAGM / pleura — costophrenic angles (blunting = effusion; track a meniscus; on supine, a hazy
     hemithorax = layering effusion); free air UNDER the diaphragm; subpulmonic effusion.
  E. EVERYTHING ELSE / EDGES — ribs, clavicles, spine, shoulders for fracture/lesion; soft tissues and
     subcutaneous gas; below the diaphragm (gastric bubble, free air); and ALL lines/tubes/devices —
     ETT, NG, central line, pacemaker, chest tube — name each and check position.

STEP 3 — GATE THE SINGLE MOST SUSPICIOUS FOCUS (protects specificity; do not emit a list of maybes)
Take the ONE strongest candidate from STEP 2 and require BOTH:
  GATE 1 (focal outlier): a DISCRETE focal density/lucency/contour break that stands apart from the
     symmetric/expected-normal pattern. A modest-but-discrete focal asymmetry still counts — do not
     dismiss a real focus for small magnitude alone (that is the under-call failure). Symmetric or
     diffusely uniform = physiologic, NOT a focal lesion.
  GATE 2 (corroboration + named-mimic veto): a SECOND independent radiographic sign at the same place
     (e.g. a pleural line AND absent peripheral markings for pneumothorax; an opacity AND a positive
     silhouette/air-bronchogram for consolidation; blunted angle AND a tracked meniscus for effusion).
     Before calling it, NAME the benign mimic and show the finding fits better: skin fold or scapular
     edge vs pneumothorax line; nipple shadow / bone island / vessel-on-end vs nodule; rotation/AP
     magnification vs cardiomegaly; overlapping ribs vs fracture. If a mimic fully explains it, it is NOT a finding.
  BOTH pass -> emit the finding with location, side, the radiographic basis, and a Tier (and a standard
     descriptor if one applies, e.g. a Lung-RADS-style note for a nodule). If the baseline under-called it, say so.
  EITHER fails -> DO NOT emit a lesion; report the worst focus's appearance AS PROOF OF NORMALITY. Do not
     lower the bar, do not promote a single sign, do not round a benign appearance toward suspicious.

NORMAL GUARD (hard rule): if a disciplined zone sweep finds no focus that clears BOTH gates, conclude a
CONFIDENT NORMAL and do NOT manufacture a finding to fill the report. There is no path from "looks
slightly off" or pressure-to-find to a positive call.

TIER FRAMEWORK (every impression line ends in one [Tier X]):
  Tier A: confirmed by 2+ independent signs (or an unambiguous classic appearance) -> "There is..."
  Tier B: a single sign / subtle -> "There is probable... / Likely..."
  Tier C: suggestive, could be a mimic/projection artifact -> "Possible... — recommend correlation / repeat view"
  Tier D: technically non-diagnostic for that structure -> "Cannot be reliably assessed due to..."
  Hard caps: a single projection without a confirming view -> Tier B max for a subtle focal call;
  any finding resting on a possible mimic -> Tier C max.
"""
