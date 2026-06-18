"""
Semantic LLM judge for MIKA reading accuracy.

Instead of keyword matching, this asks Claude (on the user's subscription, via the same CLI
auth path as the reading agent) to *understand* the known diagnosis and MIKA's read and grade
clinical equivalence — synonyms ("mass"≈"tumor", "HCC"≈"hepatocellular carcinoma"), correct
region but wrong specificity (partial), misses, and hallucinated overcalls (esp. on normal
controls). Returns a structured verdict per study.
"""
import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services.agent_runner import AgentRunner  # noqa: E402

_PROMPT = """You are a senior radiologist doing QUALITY ASSURANCE on an AI imaging report.
You are given (1) the KNOWN ground-truth for a study and (2) what the AI's report actually said.
Judge how well the AI's read matches the truth — using clinical understanding, not string matching.

Rules:
- Treat synonyms / equivalents as matches (e.g. "mass" ≈ "tumor" ≈ "neoplasm" ≈ "lesion";
  "HCC" ≈ "hepatocellular carcinoma"; "RCC"/"clear cell" ≈ "renal cell carcinoma";
  "TB" ≈ "tuberculosis"; "pulmonary nodule/opacity" can satisfy "lung cancer" if described as suspicious).
- "correct"  = the AI named the true primary finding or a clinically equivalent description,
  OR (for a NORMAL ground truth) the AI correctly called the study normal / no significant abnormality.
- "partial"  = the AI flagged an abnormality in the right organ/region but missed the specific
  diagnosis, or hedged so heavily it under-called a clear finding.
- "missed"   = the AI failed to mention the true finding / called an abnormal study normal.
- "overcall" = on a NORMAL ground truth, the AI asserted a significant disease that isn't there;
  OR it confidently invented major findings unsupported by the truth.
- Be fair about visibility: if the true finding plausibly may NOT be depicted on the single
  series/slices the AI was given, set visible_caveat=true and do not punish a reasonable miss as harshly.
- score = 0-100 clinical-usefulness of the read vs truth (100 = fully correct & specific; ~60 = right
  region, vague; 0 = wrong or dangerously misleading). Normal correctly called normal = 100.

GROUND TRUTH
  anatomy: {anatomy}
  modality: {modality}
  known diagnosis / truth: {diagnosis}
  (key positives expected: {expect_pos}; should NOT be present: {expect_neg})

WHAT THE AI'S REPORT SAID
\"\"\"
{read}
\"\"\"

Respond with ONLY a JSON object (no prose, no code fence), exactly these keys:
{{"verdict": "correct|partial|missed|overcall", "identified_primary": true/false, "score": 0-100,
"matched": ["..."], "missed": ["..."], "overcalls": ["..."], "visible_caveat": true/false,
"rationale": "one or two sentences"}}"""


def _extract_json(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except json.JSONDecodeError:
            pass
    return {"verdict": "error", "score": 0, "identified_primary": False,
            "matched": [], "missed": [], "overcalls": [], "visible_caveat": False,
            "rationale": "Judge did not return parseable JSON: " + (text or "")[:200]}


def judge(gt: dict, read_texts: list, effort: str = "medium", timeout_s: int = 600):
    """Return (verdict_dict, cost_usd). Runs one text-only Claude call on the subscription."""
    runner = AgentRunner()
    read = "\n".join(t for t in (read_texts or []) if t).strip() or "(the AI produced no findings text)"
    pos = ", ".join(f.get("label", "") for f in (gt.get("expect_findings") or [])) or "(none specified)"
    neg = ", ".join(gt.get("expect_absent") or []) or "(none specified)"
    prompt = _PROMPT.format(
        anatomy=gt.get("anatomy", "?"), modality=gt.get("modality", "?"),
        diagnosis=gt.get("diagnosis") or gt.get("notes") or "(unspecified)",
        expect_pos=pos, expect_neg=neg, read=read[:6000],
    )
    cmd = [runner.claude_bin, "-p", "--output-format", "json", "--model", runner.model, "--effort", effort]
    try:
        proc = subprocess.run(cmd, input=prompt, env=runner._child_env(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
    except Exception as e:  # noqa: BLE001
        return ({"verdict": "error", "score": 0, "identified_primary": False, "matched": [],
                 "missed": [], "overcalls": [], "visible_caveat": False,
                 "rationale": f"judge call failed: {e}"}, 0.0)
    cost = 0.0
    text = (proc.stdout or "").strip()
    try:
        env = json.loads(text)
        text = env.get("result", "") or ""
        cost = float(env.get("total_cost_usd", 0.0) or 0.0)
    except json.JSONDecodeError:
        pass
    return _extract_json(text), cost
