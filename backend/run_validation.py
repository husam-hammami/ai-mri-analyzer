"""
One-off validation: run MIKA agent mode on the real 3-study spine dataset to confirm it
produces a definitive annotated PDF like the cowork report. Runs on the Claude subscription
(API key stripped by AgentRunner). Launch in the background -- it takes many minutes.

PDF text is passed through as-is; AgentRunner feeds the prompt over a UTF-8 stdin pipe, so
private-use glyphs from the source PDFs no longer crash the Windows console encoding.
"""
import sys, json, logging, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mika.validation")

from services.agent_runner import AgentRunner

BASE = Path(r"C:\Users\husam\OneDrive\Documents\Medical_History_Full_2026")
FEB = BASE / "Husam_Hammami_MRI_Feb_21_2026"                  # 362 DICOM (contrast) -- PRIMARY
SEP = BASE / "Husam_Hammami_MRI_Sep_09_2025"                  # 317 JPG -- prior
JUN = BASE / "Husam_Hammami_MRI_NO_CONTRAST_JUNE_16_2025"     # 168 JPG -- prior

WORK = Path(__file__).resolve().parent.parent / "data" / "validation_feb2026" / "work"
WORK.mkdir(parents=True, exist_ok=True)
RESULT_JSON = WORK.parent / "agent_result.json"


def pdf_text(p: Path) -> str:
    if not p.exists():
        return ""
    try:
        import fitz
        d = fitz.open(str(p))
        return "\n".join(pg.get_text() for pg in d)
    except Exception as e:
        return f"(could not read {p.name}: {e})"


def main():
    surg = ""
    for fname in ["1st surgery_April_2025.pdf", "2nd_ surgery_Novl_2026.pdf"]:
        t = pdf_text(BASE / fname)
        if t:
            surg += f"\n--- {fname} ---\n{t}\n"
    rad = pdf_text(BASE / "MRI_Report_Feb_2026_.pdf")

    runner = AgentRunner(timeout_s=6000, effort="max")  # max effort for the accuracy goal
    log.info("AVAILABILITY: %s", json.dumps(runner.availability()))
    log.info("Surgical notes chars=%d | radiology report chars=%d", len(surg), len(rad))

    t0 = time.time()
    res = runner.run(
        study_dir=str(FEB),
        work_dir=str(WORK),
        prior_studies=[str(SEP), str(JUN)],
        clinical_history=(
            "37-year-old male. Status post L5-S1 revision microdiscectomy (Nov 2025) "
            "after a failed first discectomy (Apr 2025). Persistent left leg pain / S1 "
            "radiculopathy. Contrast (Gadolinium) administered on the Feb 2026 study."
        ),
        surgical_notes=surg or None,
        prior_reports=rad or None,
    )
    elapsed = round(time.time() - t0, 1)

    out = {
        "success": res.success,
        "elapsed_s": elapsed,
        "pdf": res.pdf_path,
        "figures": res.figures,
        "num_turns": res.num_turns,
        "cost_usd": res.cost_usd,
        "error": res.error,
        "summary_keys": list((res.summary or {}).keys()),
        "report_dir": res.report_dir,
        "result_text_tail": (res.result_text or "")[-2000:],
    }
    RESULT_JSON.write_text(json.dumps(out, indent=2), encoding="utf-8")
    log.info("DONE in %ss success=%s pdf=%s figures=%d", elapsed, res.success, res.pdf_path, len(res.figures))
    print("VALIDATION_RESULT " + json.dumps(out))


if __name__ == "__main__":
    main()
