"""
Artifact registry and QA gate for generated visuals.

The registry records generated visuals with stable metadata. The QA gate is
conservative: if a proof image is unreadable or not tied to evidence, it removes
that proof reference from the report contract instead of guessing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger("mika.artifacts")

APPROVED_BODY_MAP_ANATOMIES = {
    "spine", "brain", "msk", "chest", "abdomen", "breast", "vascular", "head_neck", "prostate"
}
PINPOINT_MARKERS = {"pin", "dot", "point", "circle", "arrow_tip", "pinpoint"}


@dataclass
class ArtifactRecord:
    artifact_id: str
    kind: str
    relative_path: str
    source: str = ""
    linked_finding_id: Optional[str] = None
    anatomy: str = "unknown"
    level: str = ""
    side: str = ""
    modality: str = ""
    sequence_view: str = ""
    calibration_state: str = "unknown"
    marker_type: str = "region"
    evidence_ids: list[str] = field(default_factory=list)
    patient_caption: str = ""
    clinician_caption: str = ""
    qa_status: str = "pending"
    qa_notes: list[str] = field(default_factory=list)
    trusted_for_proof: bool = False
    trusted_for_body_map: bool = False


class ArtifactRegistry:
    def __init__(self, work_dir: str | Path):
        self.work_dir = Path(work_dir)
        self.records: list[ArtifactRecord] = []

    def add(self, record: ArtifactRecord) -> ArtifactRecord:
        self.records.append(record)
        return record

    def add_visual(
        self,
        *,
        kind: str,
        path: str | Path,
        source: str = "",
        linked_finding_id: Optional[str] = None,
        anatomy: str = "unknown",
        level: str = "",
        side: str = "",
        modality: str = "",
        sequence_view: str = "",
        calibration_state: str = "unknown",
        marker_type: str = "region",
        evidence_ids: Optional[list[str]] = None,
        patient_caption: str = "",
        clinician_caption: str = "",
    ) -> ArtifactRecord:
        p = Path(path)
        try:
            rel = p.resolve().relative_to(self.work_dir.resolve()).as_posix()
        except Exception:
            rel = str(path)
        record = ArtifactRecord(
            artifact_id=f"art{len(self.records) + 1:03d}",
            kind=kind,
            relative_path=rel,
            source=source,
            linked_finding_id=linked_finding_id,
            anatomy=anatomy,
            level=level,
            side=side,
            modality=modality,
            sequence_view=sequence_view,
            calibration_state=calibration_state,
            marker_type=marker_type,
            evidence_ids=evidence_ids or [],
            patient_caption=patient_caption,
            clinician_caption=clinician_caption,
        )
        return self.add(record)

    def by_figure_stem(self) -> dict[str, ArtifactRecord]:
        out = {}
        for record in self.records:
            stem = Path(record.relative_path).stem
            out[stem] = record
            out[Path(record.relative_path).name] = record
        return out

    def to_manifest(self) -> dict:
        return {"manifest_version": 1, "artifacts": [asdict(r) for r in self.records]}

    def save(self) -> Path:
        out = self.work_dir / "artifacts" / "artifact_registry.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.to_manifest(), indent=2), encoding="utf-8")
        return out


class ArtifactQaGate:
    def __init__(
        self,
        work_dir: str | Path,
        evidence_manifest: Optional[dict] = None,
        approved_body_maps: Optional[set[str]] = None,
    ):
        self.work_dir = Path(work_dir)
        self.evidence_manifest = evidence_manifest or {}
        self.evidence_ids = {
            item.get("evidence_id")
            for item in (self.evidence_manifest.get("selected_images") or [])
            if item.get("evidence_id")
        }
        self.approved_body_maps = approved_body_maps or APPROVED_BODY_MAP_ANATOMIES

    def run(self, registry: ArtifactRegistry, summary: Optional[dict] = None) -> dict:
        for record in registry.records:
            self._qa_record(record)
        self._apply_summary_trust(registry, summary or {})
        registry_path = registry.save()
        result = {
            "status": "passed" if all(r.qa_status == "passed" for r in registry.records) else "limited",
            "registry_path": registry_path.relative_to(self.work_dir).as_posix(),
            "artifact_count": len(registry.records),
            "failed_artifact_count": sum(1 for r in registry.records if r.qa_status == "failed"),
            "warnings": [
                {"artifact_id": r.artifact_id, "notes": r.qa_notes}
                for r in registry.records if r.qa_notes
            ],
        }
        out = self.work_dir / "artifacts" / "artifact_qa.json"
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    def _qa_record(self, record: ArtifactRecord) -> None:
        notes = []
        if record.kind in {"proof_image", "annotated_slice", "comparison_panel", "report_figure", "pdf_figure"}:
            if not record.evidence_ids:
                notes.append("artifact has no evidence reference")
            elif not all(eid in self.evidence_ids for eid in record.evidence_ids):
                notes.append("artifact references evidence IDs not present in the manifest")
        if record.kind == "body_map" and record.anatomy not in self.approved_body_maps:
            notes.append("body-map anatomy landmark mapping is not approved")
        if record.calibration_state.lower().startswith("uncal") and record.marker_type.lower() in PINPOINT_MARKERS:
            notes.append("pinpoint marker is not allowed on uncalibrated image exports")
        image_issue = self._image_issue(record.relative_path)
        if image_issue:
            notes.append(image_issue)

        record.qa_notes = notes
        record.qa_status = "failed" if notes else "passed"
        record.trusted_for_proof = (
            record.qa_status == "passed"
            and record.kind in {"proof_image", "annotated_slice", "comparison_panel", "report_figure", "pdf_figure"}
            and bool(record.evidence_ids)
        )
        record.trusted_for_body_map = (
            record.qa_status == "passed"
            and record.kind == "body_map"
            and record.anatomy in self.approved_body_maps
            and bool(record.evidence_ids)
        )

    def _image_issue(self, rel_path: str) -> str:
        p = (self.work_dir / rel_path).resolve()
        try:
            if self.work_dir.resolve() not in p.parents or not p.is_file():
                return "artifact path is missing or outside work dir"
            img = Image.open(p)
            if img.width < 32 or img.height < 32:
                return "artifact image is too small to review"
            arr = np.asarray(img.convert("L"))
            if arr.size == 0:
                return "artifact image is blank"
            if float(arr.std()) < 1.0:
                return "artifact image is blank or unreadable"
        except Exception as e:
            return f"artifact image cannot be opened: {e}"
        return ""

    def _apply_summary_trust(self, registry: ArtifactRegistry, summary: dict) -> None:
        by_stem = registry.by_figure_stem()
        patient_findings = ((summary.get("patient") or {}).get("findings") or [])
        clinician_findings = summary.get("findings") or []
        for idx, finding in enumerate(patient_findings):
            if isinstance(finding, dict):
                self._apply_finding_trust(finding, by_stem, f"patient-{idx}")
        for idx, finding in enumerate(clinician_findings):
            if isinstance(finding, dict):
                self._apply_finding_trust(finding, by_stem, f"clinician-{idx}")

    def _apply_finding_trust(self, finding: dict, by_stem: dict[str, ArtifactRecord], fallback_id: str) -> None:
        figure = finding.get("figure") or finding.get("file")
        evidence_refs = _coerce_evidence_refs(
            finding.get("evidence_refs") or finding.get("evidence_ids") or finding.get("evidence_ref")
        )
        trust = finding.setdefault("trust", {})
        artifact = by_stem.get(Path(str(figure or "")).stem) or by_stem.get(str(figure or ""))
        artifact_refs = list(artifact.evidence_ids) if artifact else []
        effective_refs = evidence_refs or artifact_refs
        trust["valid_evidence"] = bool(effective_refs) and all(e in self.evidence_ids for e in effective_refs)
        if effective_refs and not evidence_refs:
            finding["evidence_refs"] = effective_refs
        trust["proof_image"] = bool(artifact and artifact.trusted_for_proof and trust["valid_evidence"])
        trust["body_map_marker"] = bool(trust["proof_image"] and _has_location(finding))
        if artifact and evidence_refs and not artifact.evidence_ids:
            artifact.evidence_ids = evidence_refs
        if not trust["proof_image"]:
            finding["figure"] = ""
            trust["proof_suppressed_reason"] = "No trusted evidence-linked proof image is available."
        if not trust["body_map_marker"]:
            finding["location_trusted"] = False
        finding.setdefault("finding_id", fallback_id)


def _coerce_evidence_refs(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _has_location(finding: dict) -> bool:
    return any(finding.get(k) for k in ("level", "side", "region", "location"))
