"""
Regenerate the Feb-2026 report in the patient-first, bullet-point, neutral-professional
format from the EXISTING analysis (summary.json + figures) - no imaging re-analysis.
Pure local render: no agent, no API, no cost.
"""
import sys, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from services.report_builder import build_patient_report
try:
    from prompts.base_prompt import REPORT_DISCLAIMER
except Exception:
    REPORT_DISCLAIMER = ("This analysis was generated using AI-assisted image interpretation as a "
                         "supplementary diagnostic tool. It does not replace evaluation by a board-certified "
                         "radiologist. All findings should be correlated with clinical history and examination.")

REPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "validation_feb2026" / "work" / "report"

PATIENT = {
    "patient": {"name": "Husam Ahmad Hammami", "age": "37", "sex": "Male"},
    "study": {"body_part": "Lower-back (lumbar) spine", "modality": "MRI with contrast",
              "date": "21 February 2026", "comparison": "compared with the September 2025 and June 2025 studies"},
    "bottom_line": (
        "Persistent left-leg pain is most likely due to scar tissue, and probably a small residual disc "
        "fragment, crowding the left S1 nerve at L5-S1 following the second operation."
    ),
    "key_points": [
        "Main finding: L5-S1, left side - scar tissue with a likely small residual disc fragment around the S1 nerve",
        "A newer, mild change has begun one level higher, at L4-L5",
        "The spinal canal, spinal cord and upper levels are normal",
        "No evidence of infection, fracture or tumour",
    ],
    "confidence": {"label": "High", "score": 90,
                   "note": "The principal finding is confirmed on the contrast study; a small residual disc "
                           "fragment within the scar is probable rather than certain."},
    "findings": [
        {"plain": "L5-S1: scar tissue enhances with contrast and surrounds the left S1 nerve.",
         "certainty": "Confirmed", "figure": "figure1_L5S1_enhancement.png",
         "caption": "Pre- and post-contrast at L5-S1: peri-neural tissue enhances, consistent with scar."},
        {"plain": "L5-S1: a small residual or recurrent disc fragment is likely, adding to nerve compression.",
         "certainty": "Likely", "figure": "figure2_L5S1_axial.png",
         "caption": "Axial at L5-S1: the left nerve channel is effaced; the right side is preserved."},
        {"plain": "L4-L5: a new disc bulge with facet wear mildly narrows the left nerve exit.",
         "certainty": "Confirmed", "figure": "figure3_L4L5_bulge.png",
         "caption": "L4-L5: the posterior disc margin indents the thecal sac (new since June 2025)."},
        {"plain": "Left nerve-exit foramina are narrowed at L4-L5 and L5-S1; the upper foramina are open.",
         "certainty": "Confirmed", "figure": "figure5_left_foramina.png",
         "caption": "Left para-sagittal: upper foramina patent (green); lower two narrowed (red)."},
        {"plain": "Upper discs, the spinal canal, the spinal cord and the conus are normal.",
         "certainty": "Confirmed", "figure": "figure0_level_reference.png",
         "caption": "Sagittal level reference, assigned by the sacrum-up count."},
    ],
    "change_over_time": {
        "points": [
            "The L5-S1 disc abnormality is present on all three studies (June, September, February).",
            "The second operation removed the large fragment; scar tissue and nerve crowding persist.",
            "The L4-L5 level has newly begun to change by February 2026.",
        ],
        "figure": "figure4_longitudinal.png"},
    "what_it_means": [
        "The persistent left-leg pain is consistent with compression of the left S1 nerve at L5-S1.",
        "Scar tissue is generally not re-operated; nerve-directed pain management or injection and physiotherapy may be considered.",
        "The new L4-L5 change is mild and warrants monitoring over time.",
        "Correlation with the treating spine specialist is recommended.",
    ],
    "worth_flagging": [
        "The second operative note records 'L4-L5' on one line, whereas the remainder of the note and the "
        "imaging confirm L5-S1; correction of the record is advised.",
    ],
    "disclaimer": REPORT_DISCLAIMER,
}


def main():
    old = REPORT_DIR / "report.pdf"
    if old.exists():
        try:
            shutil.copy2(str(old), str(REPORT_DIR / "report_clinical.pdf"))
        except Exception:
            pass
    out = build_patient_report(PATIENT, REPORT_DIR, REPORT_DIR / "report_patient.pdf")
    print("REBUILT:", out)


if __name__ == "__main__":
    main()
