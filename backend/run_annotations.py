"""
Focused annotation pass (loop iteration check): regenerate ONLY the annotated figures for
the Feb 2026 primary study under the Iteration-1 precision protocol, fast/cheap, so we can
converge "pixel-perfect informative annotations" without a full 34-min report each cycle.
Runs on the Claude subscription (API key stripped by AgentRunner). Background launch.
"""
import sys, json, logging, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mika.annot")

from services.agent_runner import AgentRunner, SKILL_PATH

BASE = Path(r"C:\Users\husam\OneDrive\Documents\Medical_History_Full_2026")
FEB = BASE / "Husam_Hammami_MRI_Feb_21_2026"   # 362 DICOM (contrast) -- primary
WORK = Path(__file__).resolve().parent.parent / "data" / "annot_iter" / "work"
WORK.mkdir(parents=True, exist_ok=True)
OUT = WORK / "report"
RESULT_JSON = WORK.parent / "annot_result.json"

PROMPT = f"""You are improving the ANNOTATED PROOF FIGURES for a lumbar spine MRI. Read the
annotation protocol first: {SKILL_PATH} (Phase 1 level ID, Phase 3 annotation double-check,
especially Step 3A localization, 3C pixel verification, 3D re-read, and Step 3E precision).

PRIMARY STUDY (full DICOM, contrast): {FEB}
Tools: bash, python, read, write. pydicom/numpy/scipy/Pillow installed; pip install matplotlib if needed.

PRODUCE ONLY annotated figures (no PDF, no prose report) into: {OUT}
  - figure0_level_reference.png  (midline T2 SAG, sacrum-up labels) -- the master key.
  - figure_axial_worst.png  (axial at the MOST abnormal operated level: thecal sac + BOTH
    lateral recesses, with explicit laterality patient-LEFT = image-RIGHT from DICOM IPP).
  - figure_vibe_enhancement.png (co-registered T1 VIBE fat-sat PRE vs POST gadolinium at the
    operated level, same slice index).
  - figure_left_foramina.png (left para-sagittal; foramina as REGION boxes, not pinpoints).

ANNOTATION PRECISION (mandatory, per Step 3E):
  - Localize each structure by intensity; place the tip; VERIFY its 3x3 intensity against the
    expected range; auto-search + reposition on fail; DROP any tip you cannot verify.
  - Confirm every mark's vertebral level against figure0. If unconfirmable -> region band
    "approx Lx-Ly", never a pinpoint circle.
  - Plane-shifting structures (foramina) -> REGION box. Uncalibrated images -> region bands.
  - Choose the slice where each finding is MAXIMAL.
  - Labels INFORMATIVE (structure + finding + tier + comparison e.g. "vs patent right recess")
    placed in the MARGIN with a thin leader line so text never overlaps anatomy.

Also write annotations.json: a list, one entry per drawn annotation, with keys:
  figure, label, structure, level, level_confirmed (bool), tip (or region box), intensity,
  expected_range, status (verified|repositioned|region|dropped), comparison.

When done print one JSON: {{"figures": [<paths>], "annotations": "<path to annotations.json>", "status": "complete"}}.
"""


def main():
    runner = AgentRunner(timeout_s=3600, effort="high")  # focused loop: faster than max, with headroom
    log.info("AVAILABILITY: %s", json.dumps(runner.availability()))
    t0 = time.time()
    res = runner.run(study_dir=str(FEB), work_dir=str(WORK), task_prompt=PROMPT, require_pdf=False)
    elapsed = round(time.time() - t0, 1)
    annot = OUT / "annotations.json"
    out = {
        "success": res.success, "elapsed_s": elapsed,
        "figures": res.figures, "num_turns": res.num_turns, "cost_usd": res.cost_usd,
        "error": res.error, "annotations_present": annot.exists(),
        "report_dir": res.report_dir, "result_text_tail": (res.result_text or "")[-1500:],
    }
    RESULT_JSON.write_text(json.dumps(out, indent=2), encoding="utf-8")
    log.info("DONE in %ss success=%s figures=%d", elapsed, res.success, len(res.figures))
    print("ANNOT_RESULT " + json.dumps(out))


if __name__ == "__main__":
    main()
