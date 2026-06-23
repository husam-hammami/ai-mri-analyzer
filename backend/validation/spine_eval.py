"""
Spine validation harness for MIKA.

SPIDER first, RSNA LumbarDISC staged.

Design:
- FREE layer: discover staged SPIDER cases, parse available radiological labels, and
  run MIKA detection/inventory when DICOM conversion is available.
- READING layer: optional --read invokes the same subscription Claude CLI path through
  AgentRunner, cached per case and arm.
- JUDGE layer: text-only Claude label extraction maps MIKA summaries to finding labels,
  then raw TP/FP/TN/FN counts are persisted for sensitivity and specificity.

Run from backend/:
  python -m validation.spine_eval --stage-info
  python -m validation.spine_eval --spider-root D:\\mika_datasets\\SPIDER --limit 3
  python -m validation.spine_eval --spider-root D:\\mika_datasets\\SPIDER --read --limit 3

Do not place SPIDER/RSNA data under the repo or OneDrive. Results and caches are local
validation artifacts and must not contain PHI.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.dicom_engine import DICOMEngine  # noqa: E402
from core.format_converter import FormatConverter  # noqa: E402
from services.agent_runner import AgentRunner, detect_study_modality  # noqa: E402
from validation import validate  # noqa: E402

SPIDER_RECORD_URL = "https://zenodo.org/records/8009680"
SPIDER_NEWER_RECORD_URL = "https://zenodo.org/records/10159290"
SPIDER_IMAGES_ZIP_BYTES = 3_700_000_000
SPIDER_MASKS_ZIP_BYTES = 58_200_000
SPIDER_IMAGE_ZIP_URL = "https://zenodo.org/records/8009680/files/images.zip?download=1"
SPIDER_MASK_ZIP_URL = "https://zenodo.org/records/8009680/files/masks.zip?download=1"

CACHE = HERE / "cache_spine"

FINDINGS = [
    "disc_herniation",
    "disc_bulging",
    "disc_narrowing",
    "pfirrmann_advanced",
    "modic_change",
    "spondylolisthesis",
    "endplate_change",
]

FINDING_COLUMN_HINTS = {
    "disc_herniation": ("herniation", "herniated"),
    "disc_bulging": ("bulging", "bulge"),
    "disc_narrowing": ("narrowing", "narrowed", "disc_height_loss"),
    "pfirrmann_advanced": ("pfirrmann", "pfirman"),
    "modic_change": ("modic",),
    "spondylolisthesis": ("spondylolisthesis", "listhesis"),
    "endplate_change": ("endplate", "schmorl"),
}

_EXTRACT_PROMPT = """You are mapping a lumbar-spine MRI report to validation labels.
For each finding below, output 1 if MIKA's report asserts or clearly supports the finding as
present anywhere in the lumbar spine, else 0. Treat synonyms correctly. Do not infer a label
from the reference context; use only what MIKA's report said.

FINDINGS: {labels}

MIKA REPORT:
\"\"\"
{report}
\"\"\"

Return ONLY a JSON object mapping every finding name to 0 or 1.
"""


@dataclass
class SpineCase:
    case_id: str                                          # subject id — one study per subject
    image_paths: list = field(default_factory=list)       # all sequence files for the subject (T1, T2, STIR...)
    labels: dict[str, Optional[bool]] = field(default_factory=dict)
    mask_path: Optional[Path] = None


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _cache_dir(cache_root: Path, case_id: str, arm: str) -> Path:
    suffix = "" if arm == "baseline" else f"__{arm}"
    return cache_root / (_slug(case_id) + suffix)


def _is_onedrive(path: Path) -> bool:
    return "onedrive" in str(path).lower()


def require_non_onedrive(path: Path) -> None:
    if _is_onedrive(path):
        raise ValueError(
            "Stage validation datasets outside OneDrive and outside the repo "
            f"(got {path})."
        )
    try:
        path.resolve().relative_to(REPO.resolve())
    except ValueError:
        return
    raise ValueError(f"Stage validation datasets outside the repo (got {path}).")


def bytes_to_gb(value: int) -> float:
    return round(value / (1024 ** 3), 2)


def stage_info() -> dict[str, Any]:
    return {
        "spider_record": SPIDER_RECORD_URL,
        "newer_spider_record": SPIDER_NEWER_RECORD_URL,
        "license": "CC-BY-4.0",
        "files": {
            "images.zip": {"bytes": SPIDER_IMAGES_ZIP_BYTES, "gb": bytes_to_gb(SPIDER_IMAGES_ZIP_BYTES)},
            "masks.zip": {"bytes": SPIDER_MASKS_ZIP_BYTES, "gb": bytes_to_gb(SPIDER_MASKS_ZIP_BYTES)},
        },
        "rsna_lumbardisc": {
            "status": "kaggle_or_rsna_gated",
            "note": "Stage only after accepting the dataset rules. This harness does not download gated data.",
        },
    }


def _truth_value(raw: Any, finding: str) -> Optional[bool]:
    text = str(raw or "").strip().lower()
    if text in {"", "nan", "none", "na", "n/a", "unknown", "not available"}:
        return None
    if finding == "pfirrmann_advanced":
        nums = re.findall(r"\d+(?:\.\d+)?", text)
        if nums:
            return float(nums[0]) >= 4.0
    if finding == "modic_change":
        if text in {"0", "false", "no", "normal", "absent", "none"}:
            return False
        return True
    if text in {"1", "true", "yes", "present", "positive", "y"}:
        return True
    if text in {"0", "false", "no", "normal", "absent", "negative", "n"}:
        return False
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)
    if nums:
        return float(nums[0]) > 0
    return None


def _find_column(fieldnames: list[str], hints: tuple[str, ...]) -> Optional[str]:
    lowered = {name: _slug(name) for name in fieldnames}
    for name, slug in lowered.items():
        if any(_slug(hint) in slug for hint in hints):
            return name
    return None


def _case_key_columns(fieldnames: list[str]) -> list[str]:
    preferred = [
        "study_id", "patient_id", "case_id", "study", "patient", "image", "image_path",
        "filename", "file", "series_id", "series",
    ]
    keys: list[str] = []
    slugs = {name: _slug(name) for name in fieldnames}
    for wanted in preferred:
        for name, slug in slugs.items():
            if _slug(wanted) == slug or _slug(wanted) in slug:
                if name not in keys:
                    keys.append(name)
    return keys[:3]


def load_spider_labels(root: Path) -> dict[str, dict[str, Optional[bool]]]:
    """Load SPIDER radiological gradings when their CSV/TSV is present.

    The public archive has changed filenames across mirrors. This parser is schema-tolerant:
    it searches tabular files for known finding columns and aggregates per-level rows into
    case-level labels. Positive at any IVD level makes the case positive for that finding;
    all known negatives makes it negative; otherwise unknown.
    """
    labels: dict[str, dict[str, Optional[bool]]] = {}
    tables = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".csv", ".tsv"}
        and any(tok in p.name.lower() for tok in ("grading", "grad", "label", "overview", "metadata"))
    ]
    for table in sorted(tables):
        delimiter = "\t" if table.suffix.lower() == ".tsv" else ","
        try:
            with open(table, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh, delimiter=delimiter)
                fieldnames = reader.fieldnames or []
                finding_cols = {
                    finding: _find_column(fieldnames, hints)
                    for finding, hints in FINDING_COLUMN_HINTS.items()
                }
                if not any(finding_cols.values()):
                    continue
                key_cols = _case_key_columns(fieldnames)
                for row in reader:
                    raw_key = " ".join(str(row.get(col, "")) for col in key_cols) or str(table.stem)
                    case_key = _slug(raw_key)
                    if not case_key:
                        continue
                    dst = labels.setdefault(case_key, {finding: None for finding in FINDINGS})
                    for finding, col in finding_cols.items():
                        if not col:
                            continue
                        value = _truth_value(row.get(col), finding)
                        if value is True:
                            dst[finding] = True
                        elif value is False and dst[finding] is None:
                            dst[finding] = False
        except Exception as exc:  # noqa: BLE001
            print(f"[labels] skipped {table}: {exc}")
    return labels


def _labels_for_path(path: Path, label_map: dict[str, dict[str, Optional[bool]]]) -> dict[str, Optional[bool]]:
    stem = _slug(path.stem)
    parts = [_slug(part) for part in path.parts[-4:]]
    for key, labels in label_map.items():
        if key == stem or stem in key or key in stem or any(key and key in part for part in parts):
            return dict(labels)
    return {finding: None for finding in FINDINGS}


_SEQ_SUFFIX_RE = re.compile(r"(.+?)_(?:t1|t2|stir|space|sag|ax|cor|pd|dwi|adc|t1w|t2w)\b.*$", re.I)


def _subject_id(stem: str) -> str:
    """Group key for a SPIDER file: the numeric subject id ('100' from '100_t2'), else the stem
    before a trailing sequence token, else the whole stem."""
    m = re.match(r"(\d+)", stem)
    if m:
        return m.group(1)
    m2 = _SEQ_SUFFIX_RE.match(stem)
    return m2.group(1) if m2 else stem


def _labels_for_key(key: str, label_map: dict[str, dict[str, Optional[bool]]]) -> dict[str, Optional[bool]]:
    k = _slug(key)
    for lk, labels in label_map.items():
        if lk and (lk == k or k in lk or lk in k):
            return dict(labels)
    return {finding: None for finding in FINDINGS}


def discover_spider_cases(root: Path, limit: int = 0) -> list[SpineCase]:
    require_non_onedrive(root)
    if not root.exists():
        raise FileNotFoundError(f"SPIDER root not found: {root}")
    label_map = load_spider_labels(root)
    images = [
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in {".mha", ".mhd", ".dcm", ".ima", ".dicom"}
        # Exclude segmentation files by ANY path component, not just the filename: SPIDER's masks live
        # in a masks/ dir under image-identical names (masks/1_t1.mha), so a filename-only check let
        # them through and they overwrote the grayscale in the shared study dir.
        and not any(tok in part.lower()
                    for part in p.relative_to(root).parts
                    for tok in ("mask", "seg", "label"))
    ]
    # Group every sequence file (e.g. 100_t1, 100_t2) into ONE study per subject, so the read sees
    # the full multi-sequence study instead of single sequences in isolation (input completeness).
    groups: dict[str, list[Path]] = {}
    for image in sorted(images):
        groups.setdefault(_subject_id(image.stem), []).append(image)
    cases: list[SpineCase] = []
    for subject, paths in sorted(groups.items()):
        mask = next(
            (
                p for p in paths[0].parent.rglob("*")
                if p.is_file() and p.suffix.lower() in {".mha", ".mhd", ".nii", ".gz"}
                and any(tok in p.name.lower() for tok in ("mask", "seg", "label"))
                and _subject_id(p.stem) == subject
            ),
            None,
        )
        cases.append(SpineCase(case_id=subject, image_paths=paths, mask_path=mask,
                               labels=_labels_for_key(subject, label_map)))
        if limit and len(cases) >= limit:
            break
    return cases


def _convert_mha_to_dicom(mha_path: Path, out_dir: Path) -> Path:
    try:
        import SimpleITK as sitk  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("SimpleITK is required to convert SPIDER .mha files") from exc

    image = sitk.ReadImage(str(mha_path))
    arr = sitk.GetArrayFromImage(image)  # z, y, x
    spacing = image.GetSpacing() or (1.0, 1.0, 1.0)
    has_spacing = len(spacing) >= 2 and float(spacing[0]) > 0 and float(spacing[1]) > 0
    converter = FormatConverter(str(mha_path.parent), str(out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    data = arr.astype("float32")
    if data.max() > data.min():
        data = ((data - data.min()) / (data.max() - data.min()) * 255).astype("uint8")
    else:
        data = data.astype("uint8")
    series = re.sub(r"[^A-Za-z0-9_-]+", "_", mha_path.stem) or "spider"
    for idx in range(data.shape[0]):
        converter._create_synthetic_dicom(  # noqa: SLF001 - validation adapter, not app path
            pixel_data=data[idx],
            output_path=out_dir / f"{series}_Img{idx + 1:04d}.dcm",
            series_description=series,
            instance_number=idx + 1,
            slice_location=float(idx) * (float(spacing[2]) if len(spacing) > 2 else 1.0),
            pixel_spacing=[float(spacing[1]), float(spacing[0])] if has_spacing else [1.0, 1.0],
            slice_thickness=float(spacing[2]) if len(spacing) > 2 else 1.0,
            rows=int(data.shape[1]),
            cols=int(data.shape[2]),
            study_description=f"Lumbar spine MRI (SPIDER) — {series}",
            is_calibrated=has_spacing,
            modality="MR",
        )
    return out_dir


def prepare_case(case: SpineCase, work: Path) -> Path:
    """Convert ALL of a subject's sequence files into one DICOM study dir (each .mha -> its own
    series) so the read sees the full multi-sequence study, not single sequences in isolation."""
    dicom_dir = work / "dicom"
    dicom_dir.mkdir(parents=True, exist_ok=True)
    # Same-stem files convert to identical DICOM filenames and silently overwrite each other in the
    # shared study dir (how masks/1_t1 once clobbered images/1_t1). Refuse rather than corrupt — a
    # broken study that looks like a valid read is worse than a loud failure.
    stems = [p.stem for p in case.image_paths]
    if len(set(stems)) != len(stems):
        dupes = sorted({s for s in stems if stems.count(s) > 1})
        raise ValueError(
            f"case {case.case_id}: duplicate sequence stems {dupes} would overwrite in one study dir; "
            "check the dataset layout (e.g. masks mixed with images)"
        )
    converted = False
    for image_path in case.image_paths:
        if image_path.suffix.lower() in {".mha", ".mhd"}:
            _convert_mha_to_dicom(image_path, dicom_dir)
            converted = True
        elif image_path.is_file():
            (dicom_dir / image_path.name).write_bytes(image_path.read_bytes())
            converted = True
    if not converted:
        raise FileNotFoundError(f"no usable images for case {case.case_id}")
    return dicom_dir


def score_detection(dicom_dir: Path, work: Path) -> dict[str, Any]:
    engine = DICOMEngine(str(dicom_dir), str(work / "inventory"))
    inventory = engine.run_inventory()
    try:
        modality = detect_study_modality(str(dicom_dir))
    except Exception:
        modality = "?"
    return {
        "anatomy": inventory.detected_anatomy or "unknown",
        "subregion": getattr(inventory, "anatomy_subregion", "") or "",
        "modality": modality,
        "calibrated": bool(getattr(inventory, "is_calibrated", False)),
        "sequence_count": len(inventory.sequences or {}),
    }


def report_text_of(summary: dict[str, Any]) -> str:
    patient = (summary or {}).get("patient") or {}
    parts = [str(patient.get("bottom_line") or "")]
    for key in ("findings", "impression", "incidentals", "discrepancies", "cv_supported_findings"):
        value = (summary or {}).get(key) or patient.get(key) or []
        if isinstance(value, dict):
            value = [value]
        if isinstance(value, str):
            value = [value]
        for item in value:
            if isinstance(item, dict):
                parts.append(" ".join(str(item.get(k) or "") for k in ("text", "plain", "caption", "clinician_wording")))
            else:
                parts.append(str(item))
    return "\n".join(part for part in parts if part.strip())


def extract_report_labels(report_text: str, effort: str = "low", timeout_s: int = 300) -> dict[str, int]:
    runner = AgentRunner()
    prompt = _EXTRACT_PROMPT.format(labels=", ".join(FINDINGS), report=(report_text or "")[:7000])
    cmd = [runner.claude_bin, "-p", "--output-format", "json", "--model", runner.model, "--effort", effort]
    proc = subprocess.run(
        cmd,
        input=prompt,
        env=runner._child_env(),  # noqa: SLF001 - same subscription path used by validation.llm_judge
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
    )
    text = (proc.stdout or "").strip()
    try:
        text = json.loads(text).get("result", text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    parsed = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    return {finding: 1 if int(parsed.get(finding, 0) or 0) == 1 else 0 for finding in FINDINGS}


def read_confirmed(summary: dict[str, Any], *, cached: bool, elapsed_s: float, cost_usd: float, success: bool) -> bool:
    if not success or not report_text_of(summary).strip():
        return False
    if not cached and elapsed_s < 5 and cost_usd <= 0:
        return False
    return True


def run_read(case: SpineCase, dicom_dir: Path, cache_root: Path, arm: str, force: bool) -> tuple[dict, dict]:
    cdir = _cache_dir(cache_root, case.case_id, arm)
    summary_path = cdir / "summary.json"
    meta_path = cdir / "run_meta.json"
    if summary_path.exists() and not force:
        summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) if meta_path.exists() else {}
        meta["cached"] = True
        return summary, meta
    cdir.mkdir(parents=True, exist_ok=True)
    runner = AgentRunner()
    start = time.monotonic()
    result = runner.run(
        study_dir=str(dicom_dir),
        work_dir=str(cdir / "work"),
        anatomy="spine",
        require_pdf=False,
    )
    elapsed = time.monotonic() - start
    summary = result.summary or {}
    if not summary:
        fallback = cdir / "work" / "report" / "summary.json"
        if fallback.exists():
            summary = json.loads(fallback.read_text(encoding="utf-8-sig"))
    meta = {
        "success": bool(result.success),
        "cost_usd": float(result.cost_usd or 0.0),
        "elapsed_s": round(elapsed, 2),
        "error": result.error,
        "cached": False,
        "confirmed_read": read_confirmed(
            summary,
            cached=False,
            elapsed_s=elapsed,
            cost_usd=float(result.cost_usd or 0.0),
            success=bool(result.success),
        ),
    }
    if not meta["confirmed_read"]:
        meta["error"] = meta["error"] or "read was empty, instant, or otherwise unconfirmed"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return summary, meta


def update_counts(counts: dict[str, dict[str, int]], truth: dict[str, Optional[bool]], pred: dict[str, int]) -> None:
    for finding in FINDINGS:
        expected = truth.get(finding)
        if expected is None:
            continue
        got = bool(pred.get(finding, 0))
        row = counts.setdefault(finding, {"tp": 0, "fp": 0, "tn": 0, "fn": 0})
        row["tp" if (expected and got) else "fn" if expected else "fp" if got else "tn"] += 1


def metrics_from_counts(counts: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for finding in FINDINGS:
        row = counts.get(finding, {"tp": 0, "fp": 0, "tn": 0, "fn": 0})
        tp, fp, tn, fn = row["tp"], row["fp"], row["tn"], row["fn"]
        sens_n = tp + fn
        spec_n = tn + fp
        out[finding] = {
            **row,
            "sensitivity": (tp / sens_n) if sens_n else None,
            "specificity": (tn / spec_n) if spec_n else None,
            "sensitivity_kN": f"{tp}/{sens_n}" if sens_n else "n/a",
            "specificity_kN": f"{tn}/{spec_n}" if spec_n else "n/a",
        }
    return out


def write_markdown(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "# MIKA spine validation",
        "",
        "## Scope",
        "",
        "SPIDER validates sagittal lumbar MRI disc-degeneration labels and segmentation-adjacent",
        "localization coverage. It does not validate contrast enhancement, neuritis, operative-bed",
        "scar-vs-disc distinction, or side-specific lateral recess/root involvement.",
        "",
        "## Per-finding metrics",
        "",
        "| finding | sensitivity | specificity | tp | fp | tn | fn |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for finding, metric in summary.get("per_finding", {}).items():
        sens = metric["sensitivity_kN"]
        spec = metric["specificity_kN"]
        lines.append(
            f"| {finding} | {sens} | {spec} | {metric['tp']} | {metric['fp']} | {metric['tn']} | {metric['fn']} |"
        )
    lines += ["", "## Cases", ""]
    for row in rows:
        lines.append(
            f"- {row['case_id']}: anatomy={row.get('detection', {}).get('anatomy', '?')}, "
            f"modality={row.get('detection', {}).get('modality', '?')}, "
            f"read={'cached' if row.get('read_cached') else row.get('read_status', 'not_run')}"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage-info", action="store_true", help="print SPIDER/RSNA staging info and exit")
    parser.add_argument("--spider-root", default=os.environ.get("MIKA_SPIDER_ROOT", ""))
    parser.add_argument("--cache-dir", default=str(CACHE))
    parser.add_argument("--arm", default="baseline")
    parser.add_argument("--limit", type=int, default=3, help="pilot case limit; 0 means all discovered cases")
    parser.add_argument("--read", action="store_true", help="run subscription Claude reads for uncached cases")
    parser.add_argument("--force", action="store_true", help="rerun reads and label extraction")
    parser.add_argument("--extract-effort", default="low")
    args = parser.parse_args()

    if args.stage_info:
        print(json.dumps(stage_info(), indent=2))
        return
    if not args.spider_root:
        print("ERROR: provide --spider-root or set MIKA_SPIDER_ROOT. Use --stage-info first.")
        sys.exit(2)

    root = Path(args.spider_root).expanduser()
    cache_root = Path(args.cache_dir).expanduser()
    cases = discover_spider_cases(root, limit=args.limit)
    if not cases:
        print(f"ERROR: no SPIDER image cases found under {root}")
        sys.exit(2)
    print(f"SPIDER spine validation [arm={args.arm}] - {len(cases)} cases | read={'ON' if args.read else 'off'}")

    counts: dict[str, dict[str, int]] = {finding: {"tp": 0, "fp": 0, "tn": 0, "fn": 0} for finding in FINDINGS}
    rows: list[dict[str, Any]] = []
    total_cost = 0.0
    tmp_root = Path(tempfile.mkdtemp(prefix="mika_spine_eval_"))
    try:
        for idx, case in enumerate(cases, 1):
            print(f"[{idx}/{len(cases)}] {case.case_id}")
            row: dict[str, Any] = {
                "case_id": case.case_id,
                "truth": case.labels,
                "sequences": [p.name for p in case.image_paths],
            }
            work = tmp_root / _slug(case.case_id)
            try:
                dicom_dir = prepare_case(case, work)
                row["detection"] = score_detection(dicom_dir, work)
            except Exception as exc:  # noqa: BLE001
                row["prepare_error"] = str(exc)
                print(f"  prepare error: {exc}")
                rows.append(row)
                continue

            cdir = _cache_dir(cache_root, case.case_id, args.arm)
            summary_path = cdir / "summary.json"
            labels_path = cdir / "labels.json"
            summary: dict[str, Any] = {}
            if args.read or summary_path.exists():
                summary, meta = run_read(case, dicom_dir, cache_root, args.arm, force=args.force and args.read)
                row["read_cached"] = bool(meta.get("cached"))
                row["read_status"] = "confirmed" if meta.get("confirmed_read", True) else "failed"
                row["read_cost_usd"] = float(meta.get("cost_usd") or 0.0)
                total_cost += row["read_cost_usd"]
            if summary:
                if labels_path.exists() and not args.force:
                    pred = json.loads(labels_path.read_text(encoding="utf-8-sig"))
                else:
                    pred = extract_report_labels(report_text_of(summary), effort=args.extract_effort)
                    labels_path.parent.mkdir(parents=True, exist_ok=True)
                    labels_path.write_text(json.dumps(pred, indent=2), encoding="utf-8")
                row["predicted"] = pred
                update_counts(counts, case.labels, pred)
            rows.append(row)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    per_finding = metrics_from_counts(counts)
    result = {
        "dataset": "SPIDER",
        "arm": args.arm,
        "cases": len(rows),
        "read_enabled": bool(args.read),
        "cost_usd": round(total_cost, 2),
        "scope_note": (
            "SPIDER covers sagittal lumbar MRI degeneration labels and segmentations; it does not label "
            "contrast enhancement, neuritis, scar-vs-disc, or side-specific postoperative lateral recess/root findings."
        ),
        "per_finding": per_finding,
        "rows": rows,
    }
    out_json = HERE / f"spine_results_{args.arm}.json"
    out_md = HERE / f"spine_report_{args.arm}.md"
    out_json.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    write_markdown(out_md, result, rows)
    print(json.dumps({"wrote": [str(out_json), str(out_md)], "cost_usd": result["cost_usd"]}, indent=2))


if __name__ == "__main__":
    main()
