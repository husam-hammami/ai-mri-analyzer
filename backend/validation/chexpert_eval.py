"""
CheXpert validation-set benchmark for MIKA.
==========================================
Scores MIKA's chest-X-ray reads against the radiologist-adjudicated CheXpert *validation* labels
(200 studies, 3-radiologist majority vote) — a downloadable, self-scorable gold-standard set.

Flow per study: MIKA reads the image(s) -> a text-only Claude pass maps the report to the 14
CheXpert findings (present/absent) -> compared to the ground-truth labels -> sensitivity /
specificity / accuracy / F1 per finding + overall.

Honest scope: this gives MIKA's accuracy vs radiologist-grade labels. The *published* "AI vs
radiologists head-to-head" lives on CheXpert's HIDDEN test set (submission only) — not here.

DATA (you provide): put CheXpert's `valid.csv` and the `valid/` image folder under
  test_data/chexpert/   (so test_data/chexpert/valid.csv and test_data/chexpert/valid/patient*/...)

Run from backend/ :
  MIKA_AGENT_EFFORT=high python -m validation.chexpert_eval --limit 50      # sample (recommended first)
  MIKA_AGENT_EFFORT=high python -m validation.chexpert_eval                 # all 200 (expensive)
Resumable: each read caches under validation/cache_chexpert/<study>/.
"""
import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services.agent_runner import AgentRunner          # noqa: E402
from validation import validate                        # noqa: E402

DATA = REPO / "test_data" / "chexpert"
CACHE = HERE / "cache_chexpert"

# The 14 CheXpert observations.
LABELS = [
    "No Finding", "Enlarged Cardiomediastinum", "Cardiomegaly", "Lung Opacity", "Lung Lesion",
    "Edema", "Consolidation", "Pneumonia", "Atelectasis", "Pneumothorax", "Pleural Effusion",
    "Pleural Other", "Fracture", "Support Devices",
]


def load_studies(valid_csv: Path):
    """Group CheXpert rows into studies: {study_key: {images: [paths], labels: {label: 1/0/-1/None}}}."""
    studies = {}
    with open(valid_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            path = row["Path"]
            # study key = .../patientX/studyY  (strip the viewN_*.jpg)
            key = "/".join(path.split("/")[-3:-1])
            img = (DATA / Path(*path.split("/")[1:])) if path.startswith("CheXpert") else (DATA / path)
            st = studies.setdefault(key, {"images": [], "labels": {}})
            st["images"].append(img)
            for lab in LABELS:
                v = (row.get(lab) or "").strip()
                st["labels"][lab] = (None if v == "" else int(float(v)))
    return studies


_EXTRACT = """You are mapping a chest-X-ray report to the 14 standard CheXpert findings.
For EACH finding output 1 if the report asserts or supports it as PRESENT, else 0 (absent / not mentioned).
"No Finding" = 1 only if the report says the chest is normal / no acute abnormality. Treat synonyms
correctly (e.g. "pleural effusion" ~ fluid; "cardiomegaly" ~ enlarged heart; "support devices" ~ tube/line/
pacemaker/wire). Return ONLY a JSON object mapping every finding name to 0 or 1, nothing else.

FINDINGS: {labels}

THE REPORT:
\"\"\"
{report}
\"\"\"
"""


def extract_labels(report_text: str, effort: str = "low"):
    """Map a MIKA report to a 14-finding present/absent vector via one text-only Claude call."""
    runner = AgentRunner()
    prompt = _EXTRACT.format(labels=", ".join(LABELS), report=(report_text or "")[:6000])
    cmd = [runner.claude_bin, "-p", "--output-format", "json", "--model", runner.model, "--effort", effort]
    try:
        proc = subprocess.run(cmd, input=prompt, env=runner._child_env(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=300)
        text = (proc.stdout or "").strip()
        try:
            text = json.loads(text).get("result", text)
        except json.JSONDecodeError:
            pass
        a, b = text.find("{"), text.rfind("}")
        parsed = json.loads(text[a:b + 1]) if a >= 0 and b > a else {}
    except Exception:  # noqa: BLE001
        parsed = {}
    return {lab: (1 if int(parsed.get(lab, 0) or 0) == 1 else 0) for lab in LABELS}


def report_text_of(summary: dict) -> str:
    p = (summary or {}).get("patient") or {}
    imp = summary.get("impression") or []
    if isinstance(imp, str):           # some reads emit impression as a string, not a list —
        imp = [imp]                    # iterating a string explodes it into characters ("S | i | n ...")
    parts = [p.get("bottom_line", "")] + [str(x) for x in imp]
    parts += validate.extract_finding_texts(summary)
    return "\n".join(x for x in parts if x)


def read_study(key: str, images: list, tmp: Path):
    cdir = CACHE / key.replace("/", "__")
    sfile = cdir / "summary.json"
    if sfile.exists():
        return json.loads(sfile.read_text(encoding="utf-8")), 0.0, True
    up = tmp / "u"
    up.mkdir(parents=True, exist_ok=True)
    for x in up.iterdir():
        x.unlink()
    for im in images:
        if im.exists():
            shutil.copy(im, up / im.name)
    dicom = validate.prepare_study(up, tmp / "p")
    runner = AgentRunner()
    res = runner.run(study_dir=str(dicom), work_dir=str(cdir / "work"), anatomy="chest", require_pdf=False)
    summary = res.summary or {}
    if not summary:
        f = cdir / "work" / "report" / "summary.json"
        if f.exists():
            summary = json.loads(f.read_text(encoding="utf-8-sig"))
    cdir.mkdir(parents=True, exist_ok=True)
    sfile.write_text(json.dumps(summary), encoding="utf-8")
    return summary, (res.cost_usd or 0.0), False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="score only the first N studies (0 = all)")
    ap.add_argument("--uncertain", choices=["zero", "exclude"], default="exclude",
                    help="how to treat ground-truth uncertain (-1) labels")
    args = ap.parse_args()

    valid_csv = DATA / "valid.csv"
    if not valid_csv.exists():
        print(f"ERROR: {valid_csv} not found. Put CheXpert valid.csv + valid/ under {DATA}")
        sys.exit(1)

    studies = load_studies(valid_csv)
    keys = list(studies)[: args.limit] if args.limit else list(studies)
    print(f"CheXpert benchmark — {len(keys)} studies (of {len(studies)})\n")

    stats = {lab: {"tp": 0, "fp": 0, "tn": 0, "fn": 0} for lab in LABELS}
    tmp = Path(tempfile.mkdtemp(prefix="mika_chx_"))
    total_cost = 0.0
    for i, key in enumerate(keys, 1):
        st = studies[key]
        try:
            summary, cost, cached = read_study(key, st["images"], tmp / key.replace("/", "__"))
            total_cost += cost
            lfile = CACHE / key.replace("/", "__") / "labels.json"
            if lfile.exists():
                pred = json.loads(lfile.read_text(encoding="utf-8"))
            else:
                pred = extract_labels(report_text_of(summary))
                lfile.parent.mkdir(parents=True, exist_ok=True)
                lfile.write_text(json.dumps(pred), encoding="utf-8")
            for lab in LABELS:
                gt = st["labels"].get(lab)
                if gt is None:
                    gt = 0  # blank = not asserted = negative (CheXpert convention)
                if gt == -1:
                    if args.uncertain == "exclude":
                        continue
                    gt = 0
                p = pred[lab]
                s = stats[lab]
                s["tp" if (p and gt) else "fp" if (p and not gt) else "fn" if (gt and not p) else "tn"] += 1
            print(f"  [{i}/{len(keys)}] {key} {'(cached)' if cached else f'(${cost:.2f})'}")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(keys)}] {key} ERROR {e}")

    def metrics(s):
        tp, fp, tn, fn = s["tp"], s["fp"], s["tn"], s["fn"]
        n = tp + fp + tn + fn
        sens = tp / (tp + fn) if (tp + fn) else None
        spec = tn / (tn + fp) if (tn + fp) else None
        acc = (tp + tn) / n if n else None
        return sens, spec, acc, n

    print("\n=== CheXpert per-finding (vs radiologist-consensus labels) ===")
    print(f"{'finding':30} {'sens':>6} {'spec':>6} {'acc':>6}  n")
    accs = []
    for lab in LABELS:
        sens, spec, acc, n = metrics(stats[lab])
        if acc is not None:
            accs.append(acc)
        f = lambda x: f"{100*x:.0f}%" if x is not None else "  -"  # noqa: E731
        print(f"{lab:30} {f(sens):>6} {f(spec):>6} {f(acc):>6}  {n}")
    overall = sum(accs) / len(accs) if accs else 0
    print(f"\nmacro accuracy across findings: {100*overall:.0f}%  |  credits spent: ${total_cost:.2f}")
    out = {"per_label": {lab: dict(zip(("sens", "spec", "acc", "n"), metrics(stats[lab]))) for lab in LABELS},
           "macro_accuracy": overall, "studies": len(keys), "cost_usd": round(total_cost, 2)}
    (HERE / "chexpert_results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("wrote validation/chexpert_results.json")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
