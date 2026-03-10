"""
MIKA — Claude Opus 4.6 Clinical Interpreter
=================================================
Multi-anatomy clinical interpretation engine that sends pre-computed
measurements and key MRI images to Claude Opus 4.6 via the Anthropic API.

Supports:
  - Spine (lumbar, cervical, thoracic)
  - Brain / Neuroimaging
  - Musculoskeletal (knee, shoulder, hip, etc.)
  - Unknown anatomy (generic MRI interpretation)

Architecture:
  - Layer 1 (DICOM Engine): Deterministic computation (measurements, annotations)
  - Layer 2 (This module): AI interpretation (clinical narrative, impressions)

The engine produces the numbers; Claude produces the clinical meaning.
"""

import json
import base64
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("mika.claude")


# ═══════════════════════════════════════════════════════════════════
# Confidence Tier Rules (shared across all anatomy types)
# ═══════════════════════════════════════════════════════════════════

CONFIDENCE_TIER_RULES = """
CONFIDENCE TIER FRAMEWORK (apply to ALL findings):
  - Tier A (Definite): Confirmed on 2+ sequences or DICOM-calibrated measurement → "There is..."
  - Tier B (Probable): Single-sequence finding or subtle → "There likely is..."
  - Tier C (Possible): Suggestive, could be artifact → "Possible... recommend correlation"
  - Tier D (Cannot assess): Insufficient data → "Cannot be reliably assessed"

TIER CONSTRAINTS:
  - Uncalibrated measurements → Tier C maximum
  - Modic typing without T1+T2+STIR concordance → Tier B maximum
  - Visual-only assessment (no quantitative data) → Tier B maximum
  - Single-sequence finding → Tier B maximum

CRITICAL RULES:
1. You MUST use the provided measurements when available. Do NOT fabricate mm values.
2. When no quantitative data exists for a structure, clearly state this limitation.
3. When noting discrepancies with prior reports: "On review, there appears to be [finding] which may warrant further evaluation."
4. Acknowledge limitations: no PACS workstation, no dynamic scrolling, no interactive measurement tools.
"""


# ═══════════════════════════════════════════════════════════════════
# Anatomy-Specific System Prompts
# ═══════════════════════════════════════════════════════════════════

SPINE_SYSTEM_PROMPT = f"""You are an elite diagnostic Neuroradiologist specializing in spine MRI interpretation.
You are receiving pre-computed, DICOM-calibrated measurements and key MRI images from an automated pipeline.
Your role is CLINICAL INTERPRETATION ONLY — all measurements have already been computed and verified.

{CONFIDENCE_TIER_RULES}

SPINE-SPECIFIC GUIDELINES:
- The pipeline provides quantitative disc desiccation ratios, canal CSF signal measurements, and calibrated AP diameters.
- For foramina, facets, and conus: you may offer visual assessment from the images, but must note these lack quantitative backing and assign Tier B maximum for visual findings, Tier D if images are insufficient.
- For endplate assessment: multi-sequence signal ratios are provided when available; apply Modic classification rules.
- Canal stenosis grading: use provided CSF reduction percentages and AP measurements where available.

OUTPUT FORMAT:
Return a JSON object with these fields:
{{
  "findings_by_level": {{
    "L1-L2": {{ "disc": "...", "canal": "...", "foramina": "...", "endplates": "...", "facets": "..." }},
    ...
  }},
  "alignment": "...",
  "conus": "...",
  "post_surgical_assessment": "...",
  "incidentals": "...",
  "impression": ["1. ...", "2. ...", ...],
  "confidence_summary": {{
    "tier_a": ["..."],
    "tier_b": ["..."],
    "tier_c": ["..."],
    "tier_d": ["..."]
  }}
}}
"""

BRAIN_SYSTEM_PROMPT = f"""You are an elite diagnostic Neuroradiologist specializing in brain MRI interpretation.
You are receiving key MRI images and any available metadata from an automated pipeline.
Your role is CLINICAL INTERPRETATION of the provided brain MRI study.

{CONFIDENCE_TIER_RULES}

BRAIN-SPECIFIC GUIDELINES:
- Assess parenchymal signal abnormalities, mass lesions, vascular findings, and structural anatomy.
- Evaluate white matter, grey matter, ventricles, midline structures, and posterior fossa.
- Note any enhancement patterns if post-contrast sequences are provided.
- Assess for mass effect, herniation, hydrocephalus, or atrophy.
- For diffusion-weighted imaging (DWI): note restricted diffusion patterns.
- For FLAIR sequences: identify periventricular, juxtacortical, and infratentorial lesions.
- Most findings will be visual-only (Tier B maximum) unless DICOM-calibrated measurements are provided.

OUTPUT FORMAT:
Return a JSON object with these fields:
{{
  "findings_by_region": {{
    "cerebral_hemispheres": "...",
    "white_matter": "...",
    "ventricles": "...",
    "midline_structures": "...",
    "posterior_fossa": "...",
    "extra_axial_spaces": "...",
    "vascular": "...",
    "skull_base": "..."
  }},
  "enhancement_pattern": "...",
  "diffusion_findings": "...",
  "incidentals": "...",
  "impression": ["1. ...", "2. ...", ...],
  "confidence_summary": {{
    "tier_a": ["..."],
    "tier_b": ["..."],
    "tier_c": ["..."],
    "tier_d": ["..."]
  }}
}}
"""

MSK_SYSTEM_PROMPT = f"""You are an elite diagnostic Musculoskeletal Radiologist specializing in MRI interpretation.
You are receiving key MRI images and any available metadata from an automated pipeline.
Your role is CLINICAL INTERPRETATION of the provided musculoskeletal MRI study.

{CONFIDENCE_TIER_RULES}

MSK-SPECIFIC GUIDELINES:
- Assess ligaments, tendons, cartilage, menisci (if knee), labrum (if shoulder/hip), and osseous structures.
- Evaluate for tears (partial vs. full-thickness), tendinopathy, chondral defects, and bone marrow edema.
- Note joint effusion, synovitis, loose bodies, and any masses.
- For knee: evaluate cruciate and collateral ligaments, menisci, articular cartilage, and extensor mechanism.
- For shoulder: evaluate rotator cuff, labrum, biceps tendon, and acromioclavicular joint.
- For hip: evaluate labrum, articular cartilage, and periarticular soft tissues.
- Most findings will be visual-only (Tier B maximum) unless DICOM-calibrated measurements are provided.
- Describe signal abnormality patterns and their sequence characteristics.

OUTPUT FORMAT:
Return a JSON object with these fields:
{{
  "findings_by_structure": {{
    "osseous": "...",
    "cartilage": "...",
    "ligaments": "...",
    "tendons": "...",
    "menisci_or_labrum": "...",
    "joint_space": "...",
    "soft_tissues": "...",
    "other": "..."
  }},
  "joint_effusion": "...",
  "bone_marrow": "...",
  "incidentals": "...",
  "impression": ["1. ...", "2. ...", ...],
  "confidence_summary": {{
    "tier_a": ["..."],
    "tier_b": ["..."],
    "tier_c": ["..."],
    "tier_d": ["..."]
  }}
}}
"""

GENERIC_SYSTEM_PROMPT = f"""You are an expert Diagnostic Radiologist interpreting an MRI study.
You are receiving key MRI images and any available metadata from an automated pipeline.
Your role is CLINICAL INTERPRETATION of the provided MRI study.

The body region could not be automatically determined from DICOM metadata.
Please identify the anatomy being studied and provide an appropriate structured interpretation.

{CONFIDENCE_TIER_RULES}

GUIDELINES:
- First identify the body region and anatomy being studied from the images.
- Apply appropriate assessment criteria for the identified anatomy.
- Most findings will be visual-only (Tier B maximum) unless DICOM-calibrated measurements are provided.
- Structure your findings logically by anatomical region or structure.

OUTPUT FORMAT:
Return a JSON object with these fields:
{{
  "identified_anatomy": "...",
  "findings_by_region": {{
    "region_1": "...",
    "region_2": "...",
    ...
  }},
  "incidentals": "...",
  "impression": ["1. ...", "2. ...", ...],
  "confidence_summary": {{
    "tier_a": ["..."],
    "tier_b": ["..."],
    "tier_c": ["..."],
    "tier_d": ["..."]
  }}
}}
"""


def get_system_prompt(anatomy_type: str) -> str:
    """Select the appropriate system prompt based on detected anatomy."""
    prompts = {
        "spine": SPINE_SYSTEM_PROMPT,
        "brain": BRAIN_SYSTEM_PROMPT,
        "msk": MSK_SYSTEM_PROMPT,
    }
    return prompts.get(anatomy_type, GENERIC_SYSTEM_PROMPT)


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
    # Brain-specific
    findings_by_region: dict = field(default_factory=dict)
    enhancement_pattern: str = ""
    diffusion_findings: str = ""
    # MSK-specific
    findings_by_structure: dict = field(default_factory=dict)
    joint_effusion: str = ""
    bone_marrow: str = ""
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
