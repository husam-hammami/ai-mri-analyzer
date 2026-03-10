"""
MIKA — Claude Opus 4.6 Clinical Interpreter
=================================================
Multi-anatomy clinical interpretation engine that sends pre-computed
measurements and key MRI images to Claude Opus 4.6 via the Anthropic API.

Supports 10 anatomy types with fellowship-level master prompts:
  spine, brain, msk, cardiac, chest, abdomen, breast, vascular, head_neck, prostate

Architecture:
  - Layer 1 (DICOM Engine): Deterministic computation (measurements, annotations)
  - Layer 2 (This module): AI interpretation (clinical narrative, impressions)
  - Master Prompts (backend/prompts/): Fellowship-level systematic search protocols

The engine produces the numbers; Claude produces the clinical meaning.
"""

import json
import base64
import logging
from typing import Optional
from dataclasses import dataclass, field

from backend.prompts import get_master_prompt

logger = logging.getLogger("mika.claude")


def get_system_prompt(anatomy_type: str) -> str:
    """Select the appropriate master prompt based on detected anatomy.
    Uses fellowship-level prompts from backend/prompts/ library."""
    return get_master_prompt(anatomy_type)


# ═══════════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════════

@dataclass
class InterpretationRequest:
    """Package of data sent to Claude for interpretation."""
    measurements_json: dict
    key_images_b64: dict = field(default_factory=dict)  # label -> base64 PNG
    prior_reports: Optional[str] = None
    surgical_notes: Optional[str] = None
    clinical_history: Optional[str] = None
    anatomy_type: str = "unknown"  # spine, brain, msk, unknown


@dataclass
class ClinicalInterpretation:
    """Structured output from Claude's interpretation."""
    anatomy_type: str = ""
    # Spine-specific
    findings_by_level: dict = field(default_factory=dict)
    alignment: str = ""
    conus: str = ""
    post_surgical_assessment: str = ""
    # Brain / Chest / Head-Neck / Generic region-based
    findings_by_region: dict = field(default_factory=dict)
    enhancement_pattern: str = ""
    diffusion_findings: str = ""
    # MSK / Cardiac structure-based
    findings_by_structure: dict = field(default_factory=dict)
    joint_effusion: str = ""
    bone_marrow: str = ""
    wall_motion: str = ""
    tissue_characterization: str = ""
    # Abdomen organ-based
    findings_by_organ: dict = field(default_factory=dict)
    # Vascular vessel-based
    findings_by_vessel: dict = field(default_factory=dict)
    vascular_territory: str = ""
    flow_assessment: str = ""
    # Prostate zone-based
    findings_by_zone: dict = field(default_factory=dict)
    dominant_lesion: str = ""
    pirads_category: str = ""
    extraprostatic_extension: str = ""
    # Breast-specific
    background_parenchymal_enhancement: str = ""
    kinetic_assessment: str = ""
    birads_category: str = ""
    # Head-Neck-specific
    cranial_nerves: str = ""
    # Generic
    identified_anatomy: str = ""
    # Shared
    incidentals: str = ""
    impression: list = field(default_factory=list)
    confidence_summary: dict = field(default_factory=dict)
    raw_response: str = ""
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


# ═══════════════════════════════════════════════════════════════════
# Claude Interpreter
# ═══════════════════════════════════════════════════════════════════

class ClaudeInterpreter:
    """
    Multi-anatomy clinical interpreter powered by Claude Opus 4.6.
    Automatically selects the appropriate radiological prompt template
    based on the detected anatomy type from DICOM metadata.
    """

    def __init__(self, api_key: str, model: str = "claude-opus-4-6"):
        self.api_key = api_key
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                raise RuntimeError(
                    "anthropic SDK required. Install with: pip install anthropic"
                )
        return self._client

    def interpret(self, request: InterpretationRequest) -> ClinicalInterpretation:
        """
        Send measurements and images to Claude for clinical interpretation.
        Automatically selects the appropriate prompt template for the anatomy type.
        """
        anatomy = request.anatomy_type or "unknown"
        system_prompt = get_system_prompt(anatomy)

        # Build the message content blocks
        content_blocks = []

        # 1. Measurements data (text)
        calibration = request.measurements_json.get("calibration_status", "UNCALIBRATED")
        content_blocks.append({
            "type": "text",
            "text": (
                f"## Pre-Computed Measurements (Calibration: {calibration})\n\n"
                f"```json\n{json.dumps(request.measurements_json, indent=2)}\n```\n\n"
                "These measurements were computed from DICOM metadata. "
                "Use them as-is — do not modify or fabricate alternative values. "
                "Where no quantitative data is provided for a structure, "
                "state this limitation clearly in your assessment."
            ),
        })

        # 2. Key images (vision)
        for label, b64_data in request.key_images_b64.items():
            content_blocks.append({
                "type": "text",
                "text": f"\n### Image: {label}\n",
            })
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64_data,
                },
            })

        # 3. Clinical context (if provided)
        if request.clinical_history:
            content_blocks.append({
                "type": "text",
                "text": f"\n## Clinical History\n{request.clinical_history}",
            })

        if request.surgical_notes:
            content_blocks.append({
                "type": "text",
                "text": (
                    f"\n## Surgical Notes (for post-operative reconciliation)\n"
                    f"{request.surgical_notes}"
                ),
            })

        if request.prior_reports:
            content_blocks.append({
                "type": "text",
                "text": (
                    f"\n## Prior Radiology Reports (for comparison)\n"
                    f"{request.prior_reports}"
                ),
            })

        # 4. Final instruction
        content_blocks.append({
            "type": "text",
            "text": (
                "\n\n## Task\n"
                "Analyze the provided measurements and images. "
                "Produce a clinical interpretation following the system prompt rules. "
                "Return your response as a valid JSON object matching the specified format. "
                "Every finding must include a [Tier X] confidence tag. "
                "For structures without quantitative measurements, clearly note this "
                "limitation and cap confidence at Tier B."
            ),
        })

        # Make the API call
        logger.info(f"Sending {anatomy} interpretation request to {self.model}")
        logger.info(f"  Images: {list(request.key_images_b64.keys())}")
        logger.info(f"  Measurements keys: {list(request.measurements_json.keys())}")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": content_blocks,
            }],
        )

        raw_text = response.content[0].text
        logger.info(
            f"Response received: {response.usage.input_tokens} input, "
            f"{response.usage.output_tokens} output tokens"
        )

        # Parse the JSON response
        interpretation = ClinicalInterpretation(
            anatomy_type=anatomy,
            raw_response=raw_text,
            model_used=self.model,
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

            # Parse anatomy-specific fields
            if anatomy == "spine":
                interpretation.findings_by_level = parsed.get("findings_by_level", {})
                interpretation.alignment = parsed.get("alignment", "")
                interpretation.conus = parsed.get("conus", "")
                interpretation.post_surgical_assessment = parsed.get("post_surgical_assessment", "")
            elif anatomy == "brain":
                interpretation.findings_by_region = parsed.get("findings_by_region", {})
                interpretation.enhancement_pattern = parsed.get("enhancement_pattern", "")
                interpretation.diffusion_findings = parsed.get("diffusion_findings", "")
            elif anatomy == "msk":
                interpretation.findings_by_structure = parsed.get("findings_by_structure", {})
                interpretation.joint_effusion = parsed.get("joint_effusion", "")
                interpretation.bone_marrow = parsed.get("bone_marrow", "")
            elif anatomy == "cardiac":
                interpretation.findings_by_structure = parsed.get("findings_by_structure", {})
                interpretation.wall_motion = parsed.get("wall_motion", "")
                interpretation.tissue_characterization = parsed.get("tissue_characterization", "")
            elif anatomy == "chest":
                interpretation.findings_by_region = parsed.get("findings_by_region", {})
            elif anatomy == "abdomen":
                interpretation.findings_by_organ = parsed.get("findings_by_organ", {})
                interpretation.enhancement_pattern = parsed.get("enhancement_pattern", "")
            elif anatomy == "breast":
                interpretation.findings_by_region = parsed.get("findings_by_region", {})
                interpretation.background_parenchymal_enhancement = parsed.get("background_parenchymal_enhancement", "")
                interpretation.kinetic_assessment = parsed.get("kinetic_assessment", "")
                interpretation.birads_category = parsed.get("birads_category", "")
            elif anatomy == "vascular":
                interpretation.findings_by_vessel = parsed.get("findings_by_vessel", {})
                interpretation.vascular_territory = parsed.get("vascular_territory", "")
                interpretation.flow_assessment = parsed.get("flow_assessment", "")
            elif anatomy == "head_neck":
                interpretation.findings_by_region = parsed.get("findings_by_region", {})
                interpretation.enhancement_pattern = parsed.get("enhancement_pattern", "")
                interpretation.cranial_nerves = parsed.get("cranial_nerves", "")
            elif anatomy == "prostate":
                interpretation.findings_by_zone = parsed.get("findings_by_zone", {})
                interpretation.dominant_lesion = parsed.get("dominant_lesion", "")
                interpretation.pirads_category = parsed.get("pirads_category", "")
                interpretation.extraprostatic_extension = parsed.get("extraprostatic_extension", "")
            else:
                interpretation.identified_anatomy = parsed.get("identified_anatomy", "")
                interpretation.findings_by_region = parsed.get("findings_by_region", {})

            # Shared fields
            interpretation.incidentals = parsed.get("incidentals", "")
            interpretation.impression = parsed.get("impression", [])
            interpretation.confidence_summary = parsed.get("confidence_summary", {})

        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse JSON from Claude response: {e}")
            interpretation.impression = [raw_text]

        return interpretation

    def interpret_streaming(self, request: InterpretationRequest):
        """
        Streaming version — yields partial text chunks for real-time UI updates.
        """
        anatomy = request.anatomy_type or "unknown"
        system_prompt = get_system_prompt(anatomy)

        content_blocks = []
        content_blocks.append({
            "type": "text",
            "text": (
                "## Pre-Computed Measurements\n\n"
                f"```json\n{json.dumps(request.measurements_json, indent=2)}\n```"
            ),
        })

        for label, b64_data in request.key_images_b64.items():
            content_blocks.append({"type": "text", "text": f"\n### Image: {label}\n"})
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64_data},
            })

        content_blocks.append({
            "type": "text",
            "text": (
                "\n\nAnalyze the measurements and images. "
                "Produce clinical findings with [Tier X] confidence tags. "
                "Return valid JSON."
            ),
        })

        with self.client.messages.stream(
            model=self.model,
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": content_blocks}],
        ) as stream:
            for text in stream.text_stream:
                yield text
