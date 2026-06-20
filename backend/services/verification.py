"""
VerificationPass — Senior Attending Self-Review
=================================================
Second Claude call where it acts as a senior attending radiologist
reviewing the initial report against the same images.

This catches:
  - Overcalls (artifact interpreted as pathology)
  - Grading errors (wrong Pfirrmann, wrong stenosis grade)
  - Missed anatomy (regions not addressed in the report)
  - Contradictions (finding doesn't match the images)
  - Language imprecision (vague or ambiguous statements)

This is Module 3 of Plan C+V — estimated +5-10% accuracy.
"""

import json
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("mika.verification")


# ── Data Models ──

@dataclass
class Correction:
    """A single correction made during verification."""
    finding: str
    action: str   # confirmed, downgraded, upgraded, removed, added
    reason: str


@dataclass
class VerifiedReport:
    """Output of the verification pass."""
    verified_findings: dict = field(default_factory=dict)
    corrections: list = field(default_factory=list)
    missed_findings: list = field(default_factory=list)
    audit: dict = field(default_factory=dict)            # 12-item self-audit pass/fail
    annotation_review: list = field(default_factory=list)  # per-figure 3D re-read
    cv_candidate_reviews: list = field(default_factory=list)
    quality_score: int = 0
    quality_notes: str = ""
    parsed_ok: bool = False                              # False if JSON parse failed
    raw_response: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def audit_failures(self) -> list:
        """List of audit item keys that did not pass (status == 'fail')."""
        return [k for k, v in self.audit.items() if str(v).lower() == "fail"]


# ── Verification Prompt ──

VERIFICATION_PROMPT = """
## ROLE — FINAL SELF-AUDIT (Phase 6)
You are a senior attending radiologist performing the MANDATORY final self-audit before
this report is delivered. A junior colleague produced the initial report below. You have
the SAME images, the deterministic measurements, the annotation audit trail, and the
figure inventory. Run the 12-item audit and fix any item that fails.

## THE INITIAL REPORT TO VERIFY
```json
{initial_report}
```

## DETERMINISTIC MEASUREMENTS (calibration + canal narrowing + level map)
```json
{measurements}
```

{cv_candidate_block}

## ANNOTATION AUDIT TRAIL (per arrow tip: structure, intensity, expected range, status)
```json
{annotation_audit}
```

## FIGURE INVENTORY (figure numbers you may reference / re-read)
{figure_inventory}
{prior_context}
## THE 12-ITEM AUDIT — assign each item pass | fail | na
1. mm_calibration: Every mm value is DICOM-calibrated, or is qualified as "(visual
   estimate — no calibrated measurement available)". If calibration is UNCALIBRATED, NO
   specific mm value may appear anywhere.
2. annotation_coords: Every annotation tip in the audit trail came from intensity
   analysis (status verified/repositioned), none from visual guessing.
3. annotation_intensity: No annotation has status "failed" in the audit trail. If any
   does, fail this item and note that the arrow was/should be dropped.
4. annotation_reread: Re-READ each annotated figure in the images. For EACH, confirm the
   arrow tip physically touches the intended structure, the level label is correct, and
   (axial) laterality is correct. Report per-figure in "annotation_review".
5. tier_criteria: Every confidence claim matches the tier framework (uncalibrated≤C,
   single-sequence≤B, etc.).
6. contradiction_language: Any divergence from a prior report uses soft framing
   ("appears to be … may warrant evaluation"), acknowledges the prior radiologist had
   full PACS tools, and never says "visual evidence contradicts". (na if no prior report.)
7. image_support: Every finding can point to at least one supporting figure ([See Figure N]).
8. level_counting: Recount vertebral levels from the SACRUM on the Level Reference
   (Figure 0). Every reported level label must match. (na if not a spine study.)
9. laterality: On every axial finding, patient-right = image-left. Confirm side-of-
   pathology. (na if no axial findings.)
10. incidentals_qualified: Every incidental is Tier C and ends with "dedicated imaging
    recommended for further characterization".
11. modic_concordance: Any Modic type rests on concordant T1+T2+STIR (or STIR-alone is
    only "suggestive of Modic 1" at Tier B). (na if no Modic call / not spine.)
12. enhancement_samelevel: Any enhancement/scar-vs-recurrence claim rests on a confirmed
    SAME-LEVEL pre/post comparison, else capped at Tier B. (na if no contrast.)

## CRITICAL RULES
- If you CANNOT see a reported finding in the images, DOWNGRADE or REMOVE it.
- If you see something the report MISSED, ADD it to missed_findings with a proper tier.
- When in doubt, the LESS severe interpretation wins.
- Preserve the initial report's JSON structure in "verified_findings"; only change values
  where a correction is warranted.
- CV evidence candidates are localization/measurement evidence only. Verify each candidate
  separately in "cv_candidate_reviews" using exactly one of: supported, not_supported,
  cannot_assess, localization_wrong. Reject wrong level, side, slice, or ROI with
  localization_wrong. Do not convert a candidate into a final finding unless the images
  independently support it.

{spine_block}

## OUTPUT FORMAT — return valid JSON only
{{
    "verified_findings": {{ ... corrected full findings, SAME structure as input ... }},
    "audit": {{
        "mm_calibration": "pass", "annotation_coords": "pass", "annotation_intensity": "pass",
        "annotation_reread": "pass", "tier_criteria": "pass", "contradiction_language": "na",
        "image_support": "pass", "level_counting": "pass", "laterality": "na",
        "incidentals_qualified": "pass", "modic_concordance": "na", "enhancement_samelevel": "na"
    }},
    "annotation_review": [
        {{"figure": "Figure 1", "arrow_on_target": true, "level_label_correct": true, "laterality_correct": true, "note": ""}}
    ],
    "cv_candidate_reviews": [
        {{
            "candidate_id": "candidate id",
            "status": "supported | not_supported | cannot_assess | localization_wrong",
            "evidence_refs_used": ["refs reviewed"],
            "short_reason": "brief localization/pathology review reason",
            "patient_wording": "plain-language wording if useful",
            "clinician_wording": "technical review wording"
        }}
    ],
    "corrections": [
        {{"finding": "L4-L5 disc herniation", "action": "downgraded", "reason": "Signal more consistent with Pfirrmann II on review"}}
    ],
    "missed_findings": [
        {{"finding": "T12 mild compression fracture", "tier": "B", "reason": "~20% anterior height loss on sagittal T1, STIR negative (chronic)"}}
    ],
    "quality_score": 82,
    "quality_notes": "Concise summary of the audit outcome and any failed items."
}}
"""

SPINE_AUDIT_BLOCK = """
## SPINE EMPHASIS (this IS a spine study)
- Item 8 is load-bearing: misidentifying ONE level invalidates every downstream finding.
  Physically recount from the sacrum on Figure 0 before accepting any level label.
- Item 9: reversed laterality on an axial nerve/foramen finding is a clinically dangerous
  error — verify patient-right = image-left on every axial finding.
- Item 11/12: do not let a single-sequence Modic type or a single-phase enhancement claim
  survive at high confidence.
"""


class VerificationPass:
    """
    Run a verification pass on an initial Claude interpretation.
    Sends the initial report + all images to Claude as a senior attending.
    """

    def __init__(self, api_key: str = "", model: str = "claude-opus-4-8", auth_token: str = ""):
        self.api_key = api_key
        self.auth_token = auth_token
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                from backend.services.claude_interpreter import build_anthropic_client
            except ImportError:
                from services.claude_interpreter import build_anthropic_client
            self._client = build_anthropic_client(self.api_key, self.auth_token)
        return self._client

    def verify(
        self,
        initial_report: dict,
        image_content_blocks: list[dict],
        measurements_json: dict,
        anatomy_type: str,
        annotation_audit: Optional[list] = None,
        figure_inventory: Optional[list] = None,
        prior_reports: Optional[str] = None,
        surgical_notes: Optional[str] = None,
    ) -> VerifiedReport:
        """
        Run the mandatory 12-item self-audit (skill Phase 6) on the initial report.

        Args:
            initial_report: The raw JSON from the blind-read interpretation
            image_content_blocks: Same image blocks sent to the first call (incl. figures)
            measurements_json: Deterministic measurements (calibration, canal narrowing, …)
            anatomy_type: Detected anatomy — switches in the spine-specific audit emphasis
            annotation_audit: Per-tip 3C/3D audit from the DICOM engine
            figure_inventory: List of {figure, name, description} for figure re-read
            prior_reports / surgical_notes: For the contradiction-language audit (item 6)

        Returns:
            VerifiedReport with corrections, missed findings, 12-item audit, and figure review.
        """
        spine_block = SPINE_AUDIT_BLOCK if anatomy_type == "spine" else ""

        fig_lines = []
        for fig in (figure_inventory or []):
            fig_lines.append(f"- {fig.get('figure', '?')}: {fig.get('description', fig.get('name', ''))}")
        figure_inventory_text = "\n".join(fig_lines) if fig_lines else "(no annotated figures provided)"

        prior_context = ""
        if prior_reports or surgical_notes:
            prior_context = "\n## PRIOR REPORTS / OPERATIVE NOTES (for item 6)\n"
            if prior_reports:
                prior_context += f"### Prior Radiology Reports\n{prior_reports}\n"
            if surgical_notes:
                prior_context += f"### Operative Notes\n{surgical_notes}\n"

        verification_text = VERIFICATION_PROMPT.format(
            initial_report=json.dumps(initial_report, indent=2),
            measurements=json.dumps(measurements_json, indent=2),
            cv_candidate_block=self._cv_candidate_block(measurements_json),
            annotation_audit=json.dumps(annotation_audit or [], indent=2),
            figure_inventory=figure_inventory_text,
            prior_context=prior_context,
            spine_block=spine_block,
        )

        # Build content blocks: images first, then the audit task
        content_blocks = list(image_content_blocks)
        content_blocks.append({"type": "text", "text": verification_text})

        logger.info(
            f"Running 12-item self-audit for {anatomy_type} "
            f"({len(image_content_blocks)} image blocks, "
            f"{len(annotation_audit or [])} annotations)"
        )

        response = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=(
                "You are a senior attending radiologist performing the mandatory final "
                "self-audit of an MRI interpretation. Be thorough but fair. Only correct "
                "genuine errors. Honestly mark any audit item that fails — an honest "
                "'fail' is far better than a false 'pass'."
            ),
            messages=[{"role": "user", "content": content_blocks}],
        )

        raw_text = response.content[0].text
        logger.info(
            f"Verification response: {response.usage.input_tokens} input, "
            f"{response.usage.output_tokens} output tokens"
        )

        result = VerifiedReport(
            raw_response=raw_text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

        try:
            json_str = raw_text
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]

            parsed = json.loads(json_str.strip())

            result.verified_findings = parsed.get("verified_findings", {})
            result.corrections = parsed.get("corrections", [])
            result.missed_findings = parsed.get("missed_findings", [])
            result.audit = parsed.get("audit", {})
            result.annotation_review = parsed.get("annotation_review", [])
            result.cv_candidate_reviews = self._normalize_cv_candidate_reviews(parsed.get("cv_candidate_reviews"))
            result.quality_score = parsed.get("quality_score", 0)
            result.quality_notes = parsed.get("quality_notes", "")
            result.parsed_ok = True

        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse verification JSON: {e}")
            # Fall back to the initial report unchanged, but mark the failure explicitly
            # so it is NOT indistinguishable from a passed audit downstream.
            result.verified_findings = initial_report
            result.quality_notes = f"Verification parsing failed: {e}"
            result.quality_score = 0
            result.parsed_ok = False

        return result

    @staticmethod
    def _cv_candidate_block(measurements_json: dict) -> str:
        candidates = []
        if isinstance(measurements_json, dict):
            candidates = (
                measurements_json.get("cv_candidates")
                or ((measurements_json.get("evidence_pack") or {}).get("cv_candidates") if isinstance(measurements_json.get("evidence_pack"), dict) else [])
                or []
            )
        if not candidates:
            return "## CV EVIDENCE CANDIDATES\n(no CV candidates provided)"
        return (
            "## CV EVIDENCE CANDIDATES - localization review only\n"
            "These candidates do not confirm pathology and must not overwrite the blind read.\n"
            "Review each candidate for level, side, slice/series, ROI, and visual support.\n"
            "```json\n"
            f"{json.dumps(candidates, indent=2)}\n"
            "```"
        )

    @staticmethod
    def _normalize_cv_candidate_reviews(value) -> list:
        allowed = {"supported", "not_supported", "cannot_assess", "localization_wrong"}
        if not isinstance(value, list):
            return []
        out = []
        for row in value:
            if not isinstance(row, dict) or not row.get("candidate_id"):
                continue
            status = str(row.get("status") or "").lower()
            if status not in allowed:
                status = "cannot_assess"
            refs = row.get("evidence_refs_used") or []
            if isinstance(refs, str):
                refs = [refs] if refs.strip() else []
            elif not isinstance(refs, list):
                refs = []
            out.append({
                "candidate_id": str(row.get("candidate_id")),
                "status": status,
                "evidence_refs_used": [str(ref) for ref in refs if str(ref).strip()],
                "short_reason": str(row.get("short_reason") or row.get("reason") or ""),
                "patient_wording": str(row.get("patient_wording") or ""),
                "clinician_wording": str(row.get("clinician_wording") or ""),
            })
        return out
