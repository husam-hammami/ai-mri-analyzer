"""
Run 2 local validation harness for EvidencePack and report-evidence contracts.

This script is intentionally local-only. It reads studies from caller-supplied
paths, writes generated evidence artifacts outside the repository by default,
and emits PHI-safe summary metrics only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.evidence_pack import EvidencePackBuilder  # noqa: E402


@dataclass
class ValidationCase:
    case_id: str
    label: str
    source_path: Optional[Path]
    reference_path: Optional[Path]
    expected_input_type: str
    expected_modality: str
    expected_calibrated: Optional[bool]
    expected_concepts: list[str]


def _default_output_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "MIKA" / "validation" / "run2"
    return Path.home() / ".mika" / "validation" / "run2"


def _default_data_root() -> Path:
    return Path(os.environ.get(
        "MIKA_VALIDATION_DATA_ROOT",
        str(Path.home() / "OneDrive" / "Documents" / "Medical_History_Full_2026"),
    ))


def _env_path(name: str) -> Optional[Path]:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


def _first_existing(root: Path, patterns: list[str]) -> Optional[Path]:
    if not root.exists():
        return None
    for pattern in patterns:
        matches = sorted(p for p in root.glob(pattern) if p.exists())
        if matches:
            return matches[0]
    return None


def discover_cases(data_root: Optional[Path] = None) -> list[ValidationCase]:
    root = data_root or _default_data_root()
    return [
        ValidationCase(
            case_id="feb_2026_contrast_lumbar_mri",
            label="Feb 2026 contrast lumbar MRI",
            source_path=_env_path("MIKA_VAL_FEB2026_DICOM_DIR") or _first_existing(root, ["*MRI*Feb*21*2026*", "*MRI*FEB*21*2026*"]),
            reference_path=_env_path("MIKA_VAL_FEB2026_REFERENCE") or _first_existing(root, ["*MRI_Report_Feb_2026*.pdf", "*Feb*2026*.pdf"]),
            expected_input_type="dicom",
            expected_modality="MR",
            expected_calibrated=True,
            expected_concepts=["lumbar", "contrast", "evidence_referenced"],
        ),
        ValidationCase(
            case_id="jun_2025_image_export_mri",
            label="June 2025 image-export MRI",
            source_path=_env_path("MIKA_VAL_JUN2025_IMAGE_DIR") or _first_existing(root, ["*MRI*NO*CONTRAST*JUNE*16*2025*", "*MRI*Jun*2025*"]),
            reference_path=None,
            expected_input_type="image_export",
            expected_modality="OT",
            expected_calibrated=False,
            expected_concepts=["uncalibrated", "no_precise_measurements"],
        ),
        ValidationCase(
            case_id="sep_2025_image_export_mri",
            label="Sep 2025 image-export MRI",
            source_path=_env_path("MIKA_VAL_SEP2025_IMAGE_DIR") or _first_existing(root, ["*MRI*Sep*09*2025*", "*MRI*Sep*2025*"]),
            reference_path=None,
            expected_input_type="image_export",
            expected_modality="OT",
            expected_calibrated=False,
            expected_concepts=["uncalibrated", "no_precise_measurements"],
        ),
        ValidationCase(
            case_id="xray_folder",
            label="X-ray folder",
            source_path=_env_path("MIKA_VAL_XRAY_DIR") or _first_existing(root, ["XRAY", "*XRAY*"]),
            reference_path=None,
            expected_input_type="image_export",
            expected_modality="OT",
            expected_calibrated=False,
            expected_concepts=["xray_modality", "uncalibrated_if_export"],
        ),
    ]


def _is_inside_repo(path: Path) -> bool:
    try:
        resolved = path.resolve()
        return resolved == REPO_ROOT.resolve() or REPO_ROOT.resolve() in resolved.parents
    except Exception:
        return False


def _case_report_path(report_root: Optional[Path], case_id: str) -> Optional[Path]:
    if not report_root:
        return None
    candidates = [
        report_root / case_id / "report.json",
        report_root / f"{case_id}.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _collect_report_text(report: dict) -> str:
    chunks = []
    patient = report.get("patient") or {}
    clinician = report.get("clinician") or {}
    agent = ((report.get("agent") or {}).get("summary") or {})
    for value in (
        patient.get("bottom_line"),
        patient.get("key_points"),
        patient.get("findings"),
        patient.get("what_it_means"),
        clinician.get("impression"),
        clinician.get("findings"),
        agent.get("impression"),
        agent.get("findings"),
    ):
        chunks.append(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value)
    return " ".join(chunks).lower()


def _evaluate_report_contract(report_path: Optional[Path], concepts: list[str]) -> dict:
    if not report_path or not report_path.is_file():
        return {"available": False, "flags": ["no report.json supplied for concept comparison"]}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"available": False, "flags": [f"report.json unreadable: {type(exc).__name__}"]}

    text = _collect_report_text(report)
    assets = report.get("assets") or {}
    findings = report.get("findings") or []
    flags = []
    if "evidence_referenced" in concepts and not assets.get("evidence"):
        flags.append("report missing evidence asset block")
    if any(c in concepts for c in ("uncalibrated", "no_precise_measurements")):
        bad_precision = any(
            word in text for word in ("mm", "millimeter", "pixel spacing")
        ) and "uncalibrated" in text
        if bad_precision:
            flags.append("uncalibrated report appears to include precise measurement language")
    for finding in findings:
        if isinstance(finding, dict):
            trust = finding.get("trust") or {}
            if trust and trust.get("valid_evidence") is False and finding.get("figure"):
                flags.append("finding has a proof figure despite invalid evidence")
            if finding.get("location_trusted") is False and finding.get("location"):
                flags.append("finding has a precise location despite untrusted location evidence")
    return {
        "available": True,
        "flags": sorted(set(flags)),
        "artifact_qa_status": (assets.get("artifact_qa") or {}).get("status"),
    }


def _validate_manifest(case: ValidationCase, manifest: dict) -> dict:
    study = manifest.get("study") or {}
    selected = manifest.get("selected_images") or []
    series = manifest.get("series") or []
    flags = []
    if case.expected_input_type and study.get("input_type") != case.expected_input_type:
        flags.append(f"input_type expected {case.expected_input_type}, got {study.get('input_type')}")
    if case.expected_modality and study.get("modality") != case.expected_modality:
        flags.append(f"modality expected {case.expected_modality}, got {study.get('modality')}")
    if case.expected_calibrated is not None and study.get("calibrated") is not case.expected_calibrated:
        flags.append(f"calibration expected {case.expected_calibrated}, got {study.get('calibrated')}")
    if study.get("image_count", 0) >= 80 and not (40 <= len(selected) <= 80):
        flags.append(f"large study selected {len(selected)} images, expected 40-80")
    if any(item.get("is_localizer") for item in selected) and any(not s.get("is_localizer") for s in series):
        flags.append("localizer selected despite diagnostic series being available")
    return {
        "study": {
            "input_type": study.get("input_type"),
            "modality": study.get("modality"),
            "anatomy": study.get("anatomy"),
            "subregion": study.get("subregion"),
            "calibrated": study.get("calibrated"),
            "calibration_reason": study.get("calibration_reason"),
            "series_count": study.get("series_count"),
            "image_count": study.get("image_count"),
            "selected_image_count": len(selected),
            "localizer_excluded_count": study.get("localizer_excluded_count"),
        },
        "series": [
            {
                "series_id": s.get("series_id"),
                "modality": s.get("modality"),
                "plane": s.get("plane"),
                "slice_count": s.get("slice_count"),
                "has_pixel_spacing": bool(s.get("pixel_spacing")),
                "is_localizer": bool(s.get("is_localizer")),
                "representative_count": len(s.get("representative_slice_paths") or []),
            }
            for s in series
        ],
        "limitations": manifest.get("limitations") or [],
        "flags": flags,
    }


def run_case(case: ValidationCase, output_root: Path, report_root: Optional[Path]) -> dict:
    out_dir = output_root / case.case_id
    source_available = bool(case.source_path and case.source_path.exists())
    result = {
        "case": {
            "case_id": case.case_id,
            "label": case.label,
            "source_available": source_available,
            "reference_available": bool(case.reference_path and case.reference_path.exists()),
            "expected": {
                "input_type": case.expected_input_type,
                "modality": case.expected_modality,
                "calibrated": case.expected_calibrated,
                "concepts": case.expected_concepts,
            },
        },
        "evidence": {},
        "report_contract": {},
        "status": "missing_input",
    }
    if not source_available:
        result["evidence"] = {"flags": ["source path not found; set MIKA_VALIDATION_DATA_ROOT or case-specific env vars"]}
        return result

    work_dir = out_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    pack = EvidencePackBuilder(case.source_path, work_dir).build()
    manifest = pack.to_manifest()
    manifest["manifest_path"] = str((work_dir / "evidence" / "evidence_manifest.json").relative_to(out_dir))
    result["evidence"] = _validate_manifest(case, manifest)
    result["report_contract"] = _evaluate_report_contract(
        _case_report_path(report_root, case.case_id),
        case.expected_concepts,
    )
    result["status"] = "passed" if not result["evidence"]["flags"] and not result["report_contract"].get("flags") else "flagged"
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run PHI-safe local EvidencePack validation.")
    parser.add_argument("--data-root", type=Path, default=None, help="Read-only root containing local validation studies.")
    parser.add_argument("--output-root", type=Path, default=_default_output_root(), help="Output root; defaults outside the repo.")
    parser.add_argument("--report-root", type=Path, default=None, help="Optional root containing per-case report.json files to score.")
    parser.add_argument("--case", choices=["all", "feb_2026_contrast_lumbar_mri", "jun_2025_image_export_mri", "sep_2025_image_export_mri", "xray_folder"], default="all")
    parser.add_argument("--allow-repo-output", action="store_true", help="Allow output inside this repository. Off by default.")
    args = parser.parse_args(argv)

    output_root = args.output_root
    if _is_inside_repo(output_root) and not args.allow_repo_output:
        raise SystemExit(f"Refusing to write validation artifacts inside the repo: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    cases = discover_cases(args.data_root)
    if args.case != "all":
        cases = [c for c in cases if c.case_id == args.case]
    results = [run_case(case, output_root, args.report_root) for case in cases]
    summary = {
        "harness": "run2_evidence_validation",
        "output_root": str(output_root),
        "case_count": len(results),
        "passed_count": sum(1 for r in results if r.get("status") == "passed"),
        "flagged_count": sum(1 for r in results if r.get("status") == "flagged"),
        "missing_input_count": sum(1 for r in results if r.get("status") == "missing_input"),
        "cases": results,
    }
    summary_path = output_root / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), "status": "complete"}, indent=2))
    return 0 if summary["flagged_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
