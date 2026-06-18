"""
Test the second-reader pass on the cases that matter:
  - the two weak reads (prostate-tumor miss, liver-full under-characterization) -> does sensitivity recover?
  - the two normals (shenzhen normal CXR, hippocampus)                         -> does specificity hold (no new false positives)?

Resumable: caches each second read to cache/<name>/second_read.json (skip with that present).
Run from backend/ :  MIKA_AGENT_EFFORT=high python -m validation.run_second_reader
"""
import json
import sys
import tempfile
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from validation import validate, llm_judge, second_reader  # noqa: E402

TESTS = ["tcia-qin-prostate-tumor", "tcia-tcga-lihc-liver-full",
         "nlm-shenzhen-cxr-normal", "msd-hippocampus-mr"]


def load_gt():
    studies = json.loads((HERE / "ground_truth.json").read_text(encoding="utf-8")).get("studies", [])
    gl = HERE / "ground_truth_labeled.json"
    if gl.exists():
        studies += json.loads(gl.read_text(encoding="utf-8")).get("studies", [])
    return {(s.get("name") or Path(s["path"]).name): s for s in studies}


def primary_conclusion(name: str) -> str:
    f = validate.CACHE / name / "summary.json"
    if not f.exists():
        return "(no first read available)"
    d = json.loads(f.read_text(encoding="utf-8"))
    p = d.get("patient") or {}
    parts = [p.get("bottom_line", "")] + [str(x) for x in (d.get("impression") or [])]
    return "\n".join(x for x in parts if x)[:2000]


def main():
    gt = load_gt()
    tmp = Path(tempfile.mkdtemp(prefix="mika_sr_"))
    rows = []
    for name in TESTS:
        s = gt.get(name)
        if not s:
            print(f"[{name}] not in ground truth — skip"); continue
        cdir = validate.CACHE / name
        sr_file = cdir / "second_read.json"
        if sr_file.exists():
            summary = json.loads(sr_file.read_text(encoding="utf-8"))
            print(f"[{name}] second read cached")
        else:
            work = tmp / name
            work.mkdir(parents=True, exist_ok=True)
            dicom = validate.prepare_study(validate.resolve_path(s["path"]), work)
            if not dicom:
                print(f"[{name}] could not prepare DICOM — skip"); continue
            print(f"[{name}] running second reader...")
            summary, cost, res = second_reader.run(dicom, cdir / "second_work", s, primary_conclusion(name))
            cdir.mkdir(parents=True, exist_ok=True)
            sr_file.write_text(json.dumps(summary), encoding="utf-8")
            print(f"[{name}] second read done (${cost:.2f}, success={res.success})")
        # judge the second read vs the same ground truth
        verdict, _ = llm_judge.judge(s, validate.extract_finding_texts(summary), effort="low")
        (cdir / "second_judge.json").write_text(json.dumps(verdict), encoding="utf-8")
        first = {}
        fj = cdir / "judge.json"
        if fj.exists():
            first = json.loads(fj.read_text(encoding="utf-8"))
        rows.append({
            "name": name, "truth": s.get("diagnosis", "")[:60],
            "first_verdict": first.get("verdict"), "first_score": first.get("score"),
            "second_verdict": verdict.get("verdict"), "second_score": verdict.get("score"),
            "second_bottom_line": (summary.get("patient") or {}).get("bottom_line", ""),
        })

    print("\n================ FIRST READ  vs  SECOND READER ================")
    for r in rows:
        print(f"\n{r['name']}")
        print(f"   first : {str(r['first_verdict']).upper():9} {r['first_score']}")
        print(f"   second: {str(r['second_verdict']).upper():9} {r['second_score']}")
        print(f"   second says: {r['second_bottom_line'][:160]}")
    (HERE / "second_reader_results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("\nwrote validation/second_reader_results.json")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
