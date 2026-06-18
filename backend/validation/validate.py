"""
MIKA accuracy validation harness
================================
Two layers, designed to be cheap and fast.

FREE (no Claude, runs in seconds — the default) — for every study in ground_truth.json:
  • DICOMEngine.run_inventory() + detect_study_modality()
  • score detected anatomy / modality / calibration against the ground truth
  • report sequence count

READING (opt-in, --read; costs subscription credits, ONE run per study, cached):
  • run the agent once, cache its summary.json under validation/cache/<name>/
  • score its findings against the ground-truth keyword expectations
    (recall of expected findings + overcalls of things that should be absent)
  • re-runs reuse the cache (free) unless --force

Outputs: console scorecard + validation_results.json + validation_report.md
(overall accuracy numbers + per-study right/wrong).

Run from backend/ :
  python -m validation.validate                   # FREE detection scoring (+ score any cached reads)
  python -m validation.validate --read            # also run agent reads (uncached only) — COSTS CREDITS
  python -m validation.validate --read --force     # re-run reads even if cached
  python -m validation.validate --only spine       # filter to entries whose anatomy == spine
  python -m validation.validate --max-src-mb 1000  # include large studies skipped by default

Tip: set MIKA_AGENT_EFFORT=low before --read to keep credit spend down.
"""
import os
import sys
import json
import argparse
import shutil
import tempfile
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from core.dicom_engine import DICOMEngine                       # noqa: E402
from core.format_converter import FormatConverter              # noqa: E402
from services.agent_runner import AgentRunner, detect_study_modality  # noqa: E402
from validation import llm_judge                                # noqa: E402

GT_PATH = HERE / "ground_truth.json"
CACHE = HERE / "cache"
OUT_JSON = HERE / "validation_results.json"
OUT_MD = HERE / "validation_report.md"


def resolve_path(p: str) -> Path:
    pp = Path(p)
    return pp if pp.is_absolute() else (REPO / pp)


def source_size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1e6
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def has_dicom(path: Path) -> bool:
    return path.is_dir() and any(path.rglob("*.dcm"))


def prepare_study(path: Path, work: Path) -> Optional[Path]:
    """Return a directory containing DICOM for this study, converting non-DICOM (NRRD/NIfTI/images/zip)."""
    if has_dicom(path):
        return path
    upload = work / "upload"
    upload.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        for f in path.iterdir():
            if f.is_file():
                shutil.copy(f, upload / f.name)
    elif path.is_file():
        shutil.copy(path, upload / path.name)
    else:
        return None
    dicom_dir = work / "dicom"
    dicom_dir.mkdir(parents=True, exist_ok=True)
    try:
        FormatConverter(str(upload), str(dicom_dir)).convert()
    except Exception as e:
        print(f"    [convert error] {e}")
        return None
    return dicom_dir if any(dicom_dir.rglob("*.dcm")) else None


def score_detection(dicom_dir: Path, work: Path, gt: dict) -> dict:
    eng = DICOMEngine(str(dicom_dir), str(work))
    inv = eng.run_inventory()
    got_anat = inv.detected_anatomy or "unknown"
    got_sub = getattr(inv, "anatomy_subregion", "") or ""
    try:
        got_mod = detect_study_modality(str(dicom_dir))
    except Exception:
        got_mod = "?"
    got_cal = bool(getattr(inv, "is_calibrated", False))

    def ok(field, val):
        exp = gt.get(field)
        return None if exp in (None, "") else (val == exp)

    return {
        "anatomy": got_anat, "subregion": got_sub, "modality": got_mod, "calibrated": got_cal,
        "n_sequences": len(inv.sequences or {}),
        "anatomy_ok": ok("anatomy", got_anat),
        "modality_ok": ok("modality", got_mod),
        "calibrated_ok": (None if gt.get("calibrated") is None else (got_cal == gt["calibrated"])),
    }


def extract_finding_texts(summary: dict) -> list:
    out = []
    pat = (summary or {}).get("patient") or {}
    for f in (pat.get("findings") or []):
        if isinstance(f, dict):
            out.append(((f.get("plain") or "") + " " + (f.get("caption") or "")).strip())
    if pat.get("bottom_line"):
        out.append(str(pat["bottom_line"]))
    for k in ("impression", "incidentals", "discrepancies"):
        v = (summary or {}).get(k)
        if isinstance(v, list):
            out += [str(x) for x in v]
        elif v:
            out.append(str(v))
    for f in (summary or {}).get("findings", []):
        out.append(str(f.get("text", "")) if isinstance(f, dict) else str(f))
    return [t for t in out if t and t.strip()]


def judge_reading(name: str, gt: dict, summary: dict, fresh: bool, rejudge: bool, effort: str):
    """Semantic LLM judge of the read vs known diagnosis (cached). Returns (verdict, cost_usd)."""
    jfile = CACHE / name / "judge.json"
    if jfile.exists() and not fresh and not rejudge:
        return json.loads(jfile.read_text(encoding="utf-8")), 0.0
    read_texts = extract_finding_texts(summary)
    verdict, cost = llm_judge.judge(gt, read_texts, effort=effort)
    verdict["read_excerpt"] = read_texts[:8]
    jfile.parent.mkdir(parents=True, exist_ok=True)
    jfile.write_text(json.dumps(verdict, ensure_ascii=False), encoding="utf-8")
    return verdict, cost


def run_reading(name: str, study_dir: Path, force: bool):
    cdir = CACHE / name
    cfile = cdir / "summary.json"
    if cfile.exists() and not force:
        return json.loads(cfile.read_text(encoding="utf-8")), 0.0, True
    cdir.mkdir(parents=True, exist_ok=True)
    runner = AgentRunner()  # subscription; honors MIKA_AGENT_EFFORT (set 'low' for cheap runs)
    res = runner.run(study_dir=str(study_dir), work_dir=str(cdir / "work"))
    summary = res.summary or {}
    cfile.write_text(json.dumps(summary), encoding="utf-8")
    (cdir / "run_meta.json").write_text(
        json.dumps({"success": res.success, "cost_usd": res.cost_usd, "error": res.error}), encoding="utf-8")
    return summary, (res.cost_usd or 0.0), False


def _mark(b):
    return "—" if b is None else ("OK" if b else "X")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--read", action="store_true", help="run the agent read (COSTS CREDITS) for uncached studies")
    ap.add_argument("--force", action="store_true", help="re-run reads even if cached")
    ap.add_argument("--only", default="", help="filter to entries whose anatomy == this")
    ap.add_argument("--max-src-mb", type=float, default=150.0,
                    help="skip converting non-DICOM studies larger than this (keeps the free run fast)")
    ap.add_argument("--rejudge", action="store_true", help="re-run the LLM judge even if a cached verdict exists")
    ap.add_argument("--judge-effort", default="medium", help="effort for the LLM judge (low|medium|high)")
    args = ap.parse_args()

    studies = json.loads(GT_PATH.read_text(encoding="utf-8")).get("studies", [])
    gt_labeled = HERE / "ground_truth_labeled.json"
    if gt_labeled.exists():  # auto-generated by fetch_labeled.py — labeled studies with known diagnoses
        studies += json.loads(gt_labeled.read_text(encoding="utf-8")).get("studies", [])
    if args.only:
        studies = [s for s in studies if s.get("anatomy") == args.only]

    results = []
    total_cost = 0.0
    tmpbase = Path(tempfile.mkdtemp(prefix="mika_val_"))
    print(f"\nMIKA validation — {len(studies)} studies | read={'ON (credits)' if args.read else 'off'}\n")

    for s in studies:
        name = s.get("name") or Path(s["path"]).name
        path = resolve_path(s["path"])
        row = {"name": name, "path": str(path)}
        print(f"- {name}")
        if not path.exists():
            row["error"] = "path not found"
            print("    [skip] path not found")
            results.append(row)
            continue
        work = tmpbase / name
        work.mkdir(parents=True, exist_ok=True)
        if not has_dicom(path):
            sz = source_size_mb(path)
            if sz > args.max_src_mb:
                row["skipped_large_mb"] = round(sz, 1)
                print(f"    [skip] {sz:.0f} MB non-DICOM > --max-src-mb {args.max_src_mb:.0f}")
                results.append(row)
                continue
        try:
            dicom_dir = prepare_study(path, work)
        except Exception as e:
            row["error"] = f"prepare: {e}"
            print(f"    [error] {e}")
            results.append(row)
            continue
        if not dicom_dir:
            row["error"] = "no DICOM after prepare"
            print("    [error] no DICOM produced")
            results.append(row)
            continue

        det = score_detection(dicom_dir, work, s)
        row["detection"] = det
        print(f"    anatomy {_mark(det['anatomy_ok'])} {det['anatomy']}"
              f"{('/' + det['subregion']) if det['subregion'] else ''} (exp {s.get('anatomy', '?')}) | "
              f"modality {_mark(det['modality_ok'])} {det['modality']} (exp {s.get('modality', '?')}) | "
              f"cal {_mark(det['calibrated_ok'])} {det['calibrated']} | seq {det['n_sequences']}")

        # Only spend credits reading studies we can actually score (have a known diagnosis);
        # always (re)score a study that already has a cached read.
        has_truth = bool(s.get("expect_findings")) or bool(s.get("diagnosis"))
        if (args.read and has_truth) or (CACHE / name / "summary.json").exists():
            try:
                summary, cost, cached = run_reading(name, dicom_dir, args.force and args.read)
                total_cost += cost
                verdict, jcost = judge_reading(name, s, summary, fresh=not cached,
                                               rejudge=args.rejudge, effort=args.judge_effort)
                total_cost += jcost
                row["reading"] = verdict
                row["reading_cached"] = cached
                row["reading_cost_usd"] = round(cost + jcost, 2)
                tag = "(read cached)" if cached else f"(read ${cost:.2f})"
                print(f"    reading {tag}: {str(verdict.get('verdict', '?')).upper()} "
                      f"score {verdict.get('score', '?')}/100 — {str(verdict.get('rationale', ''))[:160]}")
                if verdict.get("overcalls"):
                    print(f"      OVERCALLS: {', '.join(verdict['overcalls'])}")
            except Exception as e:
                row["reading_error"] = str(e)
                print(f"    [reading error] {e}")
        results.append(row)

    det_rows = [r for r in results if r.get("detection")]

    def acc(field):
        vals = [r["detection"][field] for r in det_rows if r["detection"].get(field) is not None]
        return sum(1 for v in vals if v), len(vals)

    a_hit, a_n = acc("anatomy_ok")
    m_hit, m_n = acc("modality_ok")
    c_hit, c_n = acc("calibrated_ok")
    read_rows = [r for r in results if r.get("reading") and r["reading"].get("verdict") not in (None, "error")]
    verdicts = [r["reading"]["verdict"] for r in read_rows]
    n_correct, n_partial = verdicts.count("correct"), verdicts.count("partial")
    n_missed, n_overcall = verdicts.count("missed"), verdicts.count("overcall")
    scores = [r["reading"].get("score", 0) or 0 for r in read_rows]
    mean_score = round(sum(scores) / len(scores)) if scores else 0
    diag = n_correct + 0.5 * n_partial  # partial credit for right-region-wrong-specificity

    def pct(h, n):
        return f"{(100 * h / n):.0f}% ({h}/{n})" if n else "n/a"

    summary = {
        "studies_scored": len(det_rows),
        "anatomy_accuracy": pct(a_hit, a_n),
        "modality_accuracy": pct(m_hit, m_n),
        "calibration_accuracy": pct(c_hit, c_n),
        "reading_judged": len(read_rows),
        "reading_diagnostic_accuracy": (f"{100 * diag / len(read_rows):.0f}% "
                                        f"({n_correct} correct + {n_partial} partial of {len(read_rows)})"
                                        if read_rows else "n/a"),
        "reading_mean_score": f"{mean_score}/100" if read_rows else "n/a",
        "reading_verdicts": f"correct {n_correct} · partial {n_partial} · missed {n_missed} · overcall {n_overcall}",
        "credit_spent_usd": round(total_cost, 2),
    }
    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k:26}: {v}")

    OUT_JSON.write_text(json.dumps({"summary": summary, "results": results}, indent=2, default=str), encoding="utf-8")
    _write_markdown(summary, results)
    print(f"\nWrote {OUT_JSON.relative_to(REPO)} and {OUT_MD.relative_to(REPO)}")
    shutil.rmtree(tmpbase, ignore_errors=True)


def _write_markdown(summary, results):
    L = ["# MIKA validation report", "", "## Accuracy", "", "| metric | value |", "|---|---|"]
    for k, v in summary.items():
        L.append(f"| {k.replace('_', ' ')} | {v} |")
    L += ["", "## Per-study", ""]
    for r in results:
        L.append(f"### {r['name']}")
        if r.get("error"):
            L.append(f"- ⚠ {r['error']}")
        if r.get("skipped_large_mb"):
            L.append(f"- skipped (large: {r['skipped_large_mb']} MB; use --max-src-mb to include)")
        d = r.get("detection")
        if d:
            L.append(f"- anatomy {_mark(d['anatomy_ok'])} **{d['anatomy']}**"
                     f"{('/' + d['subregion']) if d['subregion'] else ''} | "
                     f"modality {_mark(d['modality_ok'])} **{d['modality']}** | "
                     f"calibrated {_mark(d['calibrated_ok'])} {d['calibrated']} | sequences {d['n_sequences']}")
        rd = r.get("reading")
        if rd and rd.get("verdict"):
            cav = " _(finding may not be depicted on the provided series)_" if rd.get("visible_caveat") else ""
            L.append(f"- reading: **{str(rd['verdict']).upper()}** ({rd.get('score', '?')}/100){cav} — "
                     f"{rd.get('rationale', '')}")
            if rd.get("matched"):
                L.append(f"  - got right: {', '.join(rd['matched'])}")
            if rd.get("missed"):
                L.append(f"  - missed: {', '.join(rd['missed'])}")
            if rd.get("overcalls"):
                L.append(f"  - OVERCALLS: {', '.join(rd['overcalls'])}")
            if rd.get("read_excerpt"):
                L.append("  - MIKA said: " + " | ".join(t[:120] for t in rd["read_excerpt"][:6]))
        L.append("")
    OUT_MD.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
