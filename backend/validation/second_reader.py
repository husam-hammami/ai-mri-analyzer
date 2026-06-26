"""
Second-reader sensitivity pass — runs through the SAME subscription pipeline (AgentRunner / claude CLI)
so it can see the images, but with a sensitivity-tuned task_prompt that challenges the first read's
under-calls (two-gate decision: relative focal-outlier + same-location corroboration, with a hard
normal-guard). Returns a summary.json-shaped dict the validation judge can score.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services.agent_runner import AgentRunner          # noqa: E402
from validation import second_reader_prompt as srp     # noqa: E402


def _hunt_for(anatomy: str) -> str:
    if anatomy in srp.HUNT_BLOCKS:
        return srp.HUNT_BLOCKS[anatomy]   # prostate / abdomen / chest get a dedicated hunt
    return srp.HUNT_BLOCKS["generic"]


def run(study_dir, work_dir, gt: dict, primary_conclusion: str):
    """Run the second reader. Returns (summary_dict, cost_usd, AgentResult)."""
    anatomy = gt.get("anatomy") or "unknown"
    modality = gt.get("modality") or "MR"
    work = Path(work_dir)
    out_dir = work / "report"
    prompt = srp.TEMPLATE.format(
        anatomy=anatomy, modality=modality, study_dir=str(study_dir),
        out_dir=str(out_dir), primary_conclusion=primary_conclusion or "(no first read available)",
        hunt_block=_hunt_for(anatomy),
    )
    runner = AgentRunner()  # honors MIKA_AGENT_EFFORT
    res = runner.run(study_dir=str(study_dir), work_dir=str(work), anatomy=anatomy,
                     task_prompt=prompt, require_pdf=False)
    summary = res.summary or {}
    if not summary:  # salvage if the orchestrator stalled after the agent wrote the file
        f = out_dir / "summary.json"
        if f.exists():
            summary = json.loads(f.read_text(encoding="utf-8-sig"))
    return summary, (res.cost_usd or 0.0), res
