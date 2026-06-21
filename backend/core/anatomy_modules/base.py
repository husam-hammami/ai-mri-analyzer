"""
Shared contracts for deterministic anatomy evidence modules.

Candidates are localization prompts for Claude/verifier review. They are not
diagnoses and must never be merged into final confirmed findings without an
explicit verifier status.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from core.study_graph import StudyGraph


VERIFIER_STATUSES = ("supported", "not_supported", "cannot_assess", "localization_wrong", "unstable")
TRUSTED_GEOMETRY_CONFIDENCE = 0.80
TRUSTED_REGISTRATION_CONFIDENCE = 0.80


@dataclass
class EvidenceCandidate:
    candidate_id: str
    anatomy: str
    level: str
    side: str
    series_ids: list[str]
    slice_ids: list[str]
    candidate_type: str
    roi: dict[str, Any]
    calibration_state: str
    geometry_confidence: float
    registration_confidence: float
    limitations: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    selected_evidence_refs: list[str] = field(default_factory=list)
    physical_pair_distances: list[dict[str, Any]] = field(default_factory=list)
    registration_qc: dict[str, Any] = field(default_factory=dict)
    adjacent_slice_refs: list[str] = field(default_factory=list)
    proof_bundle: dict[str, Any] = field(default_factory=dict)
    contrast_timing: dict[str, Any] = field(default_factory=dict)
    bounded_question: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["requires_verifier"] = True
        data["cv_claim_scope"] = "localization_only"
        return data


@dataclass
class EvidenceCandidateSet:
    module: str
    candidates: list[EvidenceCandidate] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    contract_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "module": self.module,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "limitations": self.limitations,
            "verifier_contract": candidate_verifier_contract(),
            "artifact_policy": {
                "proof_overlay": "only for candidates passing geometry and registration trust gates",
                "body_marker": "only for calibrated candidates with high level/side confidence",
                "pinpoint_marker": "never for uncalibrated image exports",
                "final_claim": "CV candidates alone cannot create final visual claims",
            },
        }


class AnatomyEvidenceModule(ABC):
    module_id: str

    @abstractmethod
    def analyze(self, study_graph: StudyGraph) -> EvidenceCandidateSet:
        """Return deterministic evidence candidates and limitations."""


def candidate_verifier_contract() -> dict[str, Any]:
    return {
        "input": "candidate_rois",
        "allowed_statuses": list(VERIFIER_STATUSES),
        "required_fields": [
            "candidate_id",
            "status",
            "evidence_refs_used",
            "short_reason",
            "pre_post_enhancement_support",
            "level_side_localization",
            "visible_evidence_reason",
            "patient_wording",
            "clinician_wording",
        ],
        "rules": [
            "Classify only whether the candidate ROI supports the described localization target.",
            "Use localization_wrong when the level, side, series, or slice does not match.",
            "Use cannot_assess when images, registration, sequence, or metadata are insufficient.",
            "Use unstable when repeated focused checks or visible evidence are mixed.",
            "Do not treat CV localization as a diagnosis or as independent confirmation of pathology.",
            "Do not classify scar versus recurrent disc or nerve-root encasement from CV metadata alone.",
            "Do not make broad negative statements from a bounded candidate review.",
        ],
    }


def candidate_allows_body_marker(candidate: EvidenceCandidate) -> bool:
    return (
        candidate.calibration_state == "calibrated"
        and bool(candidate.level)
        and bool(candidate.side)
        and candidate.geometry_confidence >= TRUSTED_GEOMETRY_CONFIDENCE
    )


def candidate_allows_proof_overlay(candidate: EvidenceCandidate) -> bool:
    return (
        candidate_allows_body_marker(candidate)
        and candidate.registration_confidence >= TRUSTED_REGISTRATION_CONFIDENCE
        and not any("cannot assess" in note.lower() or "failed" in note.lower() for note in candidate.limitations)
    )


def candidate_allows_pinpoint_marker(candidate: EvidenceCandidate) -> bool:
    return candidate_allows_proof_overlay(candidate) and candidate.calibration_state == "calibrated"
