"""
SpineAI — Claude Opus 4.6 Clinical Interpreter
=================================================
This service sends pre-computed measurements and key MRI images
to Claude Opus 4.6 via the Anthropic API for clinical interpretation.

Architecture:
  - Layer 1 (DICOM Engine): Deterministic computation (measurements, annotations)
  - Layer 2 (This module): AI interpretation (clinical narrative, Modic typing, impressions)

The engine produces the numbers; Claude produces the clinical meaning.
"""

import json
import base64
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("spineai.claude")

# System prompt that embeds the full neuroradiology protocol
SYSTEM_PROMPT = """You are an elite diagnostic Neuroradiologist conducting an MRI lumbar spine analysis.
You are receiving pre-computed, DICOM-calibrated measurements and key MRI images from an automated pipeline.
Your role is CLINICAL INTERPRETATION ONLY — all measurements have already been computed and verified.

CRITICAL RULES:
1. You MUST use the provided measurements. Do NOT fabricate your own mm values.
2. Every finding gets a confidence tier:
   - Tier A (Definite): Confirmed on 2+ sequences or DICOM-calibrated measurement → "There is..."
   - Tier B (Probable): Single-sequence or subtle → "There likely is..."
   - Tier C (Possible): Suggestive, could be artifact → "Possible... recommend correlation"
   - Tier D (Cannot assess): Insufficient data → "Cannot be reliably assessed"
3. Uncalibrated measurements = Tier C max.
4. Modic typing without T1+T2+STIR concordance = Tier B max.
5. When noting discrepancies with prior reports, use: "On review, there appears to be [finding] which may warrant further evaluation."
6. Acknowledge limitations: no PACS workstation, no dynamic scrolling, no interactive measurement tools.

OUTPUT FORMAT:
Return a JSON object with these fields:
{
  "findings_by_level": {
    "L1-L2": { "disc": "...", "canal": "...", "foramina": "...", "endplates": "...", "facets": "..." },
    ...
  },
  "alignment": "...",
  "conus": "...",
  "post_surgical_assessment": "...",
  "incidentals": "...",
  "impression": ["1. ...", "2. ...", ...],
  "confidence_summary": {
    "tier_a": ["..."],
    "tier_b": ["..."],
    "tier_c": ["..."],
    "tier_d": ["..."]
  }
}
"""


@dataclass
class InterpretationRequest:
    """Package of data sent to Claude for interpretation."""
    measurements_json: dict
    key_images_b64: dict = field(default_factory=dict)  # label -> base64 PNG
    prior_reports: Optional[str] = None
    surgical_notes: Optional[str] = None
    clinical_history: Optional[str] = None


@dataclass
class ClinicalInterpretation:
    """Structured output from Claude's interpretation."""
    findings_by_level: dict = field(default_factory=dict)
    alignment: str = ""
    conus: str = ""
    post_surgical_assessment: str = ""
    incidentals: str = ""
    impression: list = field(default_factory=list)
    confidence_summary: dict = field(default_factory=dict)
    raw_response: str = ""
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class ClaudeInterpreter:
    """
    Sends measurement data and images to Claude Opus 4.6 for clinical interpretation.
    Requires an Anthropic API key.
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
        Returns structured clinical findings.
        """
        # Build the message content blocks
        content_blocks = []

        # 1. Measurements data (text)
        content_blocks.append({
            "type": "text",
            "text": (
                "## Pre-Computed DICOM-Calibrated Measurements\n\n"
                f"```json\n{json.dumps(request.measurements_json, indent=2)}\n```\n\n"
                "These measurements were computed from DICOM PixelSpacing metadata. "
                "Use them as-is — do not modify or fabricate alternative values."
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
                    f"\n## Surgical Notes (for Phase 5 reconciliation)\n"
                    f"{request.surgical_notes}"
                ),
            })

        if request.prior_reports:
            content_blocks.append({
                "type": "text",
                "text": (
                    f"\n## Prior Radiology Reports (for Phase 5 reconciliation)\n"
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
                "Every finding must include a [Tier X] confidence tag."
            ),
        })

        # Make the API call
        logger.info(f"Sending interpretation request to {self.model}")
        logger.info(f"  Images: {list(request.key_images_b64.keys())}")
        logger.info(f"  Measurements keys: {list(request.measurements_json.keys())}")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
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
            raw_response=raw_text,
            model_used=self.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

        try:
            # Extract JSON from response (Claude may wrap it in markdown code fences)
            json_str = raw_text
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]

            parsed = json.loads(json_str.strip())
            interpretation.findings_by_level = parsed.get("findings_by_level", {})
            interpretation.alignment = parsed.get("alignment", "")
            interpretation.conus = parsed.get("conus", "")
            interpretation.post_surgical_assessment = parsed.get("post_surgical_assessment", "")
            interpretation.incidentals = parsed.get("incidentals", "")
            interpretation.impression = parsed.get("impression", [])
            interpretation.confidence_summary = parsed.get("confidence_summary", {})

        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse JSON from Claude response: {e}")
            # Fall back to raw text interpretation
            interpretation.impression = [raw_text]

        return interpretation

    def interpret_streaming(self, request: InterpretationRequest):
        """
        Streaming version — yields partial text chunks for real-time UI updates.
        Useful for showing the user that analysis is in progress.
        """
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
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content_blocks}],
        ) as stream:
            for text in stream.text_stream:
                yield text
