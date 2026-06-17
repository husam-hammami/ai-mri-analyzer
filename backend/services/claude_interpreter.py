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

try:
    from backend.prompts import get_master_prompt
except ImportError:
    from prompts import get_master_prompt

logger = logging.getLogger("mika.claude")


def build_anthropic_client(api_key: str = "", auth_token: str = ""):
    """
    Construct an Anthropic client from whatever credential is available, in priority order:
      1. an explicit API key (console.anthropic.com — pay-per-token Developer Platform)
      2. an explicit bearer / OAuth token (e.g. from a Claude subscription via Claude Code
         or `ant auth login`) — sent as Authorization: Bearer with the oauth beta header
      3. the environment / login profile: ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
         `ant auth login` profile — the SDK resolves these when no args are passed.

    This lets MIKA run on a metered API key OR a subscription-derived token without code
    changes. Note: subscription tokens are short-lived (auto-refresh only via the profile),
    usage-capped, and may be outside a consumer subscription's intended use — confirm your
    plan permits programmatic API access before relying on it.
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK required. Install with: pip install anthropic")

    if api_key:
        return anthropic.Anthropic(api_key=api_key)
    if auth_token:
        # Bearer/OAuth tokens require the oauth beta header on the Messages API.
        return anthropic.Anthropic(
            auth_token=auth_token,
            default_headers={"anthropic-beta": "oauth-2025-04-20"},
        )
    # Resolve from ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login` profile.
    return anthropic.Anthropic()


def _modality_note(modality: str) -> str:
    """Modality discipline note appended to the master prompt so a non-MR study (CT, X-ray,
    ultrasound, ...) is not interpreted with MRI sequence logic. Empty for MR."""
    mod = (modality or "MR").upper()
    if mod == "MR":
        return ""
    try:
        from services.agent_runner import MODALITY_LABELS, MODALITY_READING
    except ImportError:
        from backend.services.agent_runner import MODALITY_LABELS, MODALITY_READING
    label = MODALITY_LABELS.get(mod, mod)
    how = MODALITY_READING.get(mod, "Apply interpretation appropriate to this modality.")
    return (f"\n\n## MODALITY — this is a {label} study (DICOM Modality: {mod}), NOT MRI\n"
            f"- {how}\n"
            f"- The grading tables above are MRI-tuned. Use the anatomical search checklist and "
            f"confidence-tier discipline (modality-independent), but DO NOT assume MRI pulse "
            f"sequences (T1/T2/STIR/FLAIR) and DO NOT report MR signal characteristics — they do "
            f"not exist on {label}. Report only what this modality shows.\n")


def get_system_prompt(anatomy_type: str, modality: str = "MR") -> str:
    """Select the appropriate master prompt based on detected anatomy, plus a modality
    discipline note. Uses fellowship-level prompts from backend/prompts/ library."""
    return get_master_prompt(anatomy_type) + _modality_note(modality)


# Skill Phase 5: surgical reconciliation. Run ONLY AFTER the blind read so the prior
# reports/surgical notes cannot anchor the independent image interpretation.
RECONCILIATION_PROMPT = """
## ROLE — POST-READ RECONCILIATION (Phase 5)
You have ALREADY completed an independent blind read of this study (provided below as
JSON). NOW, and only now, you are given the surgical notes and/or prior radiology
reports. Compare your blind read against them and produce a reconciliation.

## YOUR INDEPENDENT BLIND READ
```json
{blind_report}
```

## RULES (do not violate)
- Do NOT silently rewrite your blind read to agree with the prior report. Keep your
  independent findings; only flag genuine differences.
- When your read differs from a prior radiology report, frame it softly:
  "On review, there appears to be [finding] which may warrant further evaluation — this
  was not included in the [date] report by [institution]."
- ACKNOWLEDGE that the reporting radiologist had a full PACS workstation, measurement
  calipers, and clinical context this analysis did not.
- NEVER write "visual evidence contradicts" or equivalently strong phrasing.
- Textual discrepancies WITHIN an operative report (e.g. procedure name says one level,
  narrative describes another) are factual observations — report them directly.
- Differences vs surgical findings: the surgeon had direct visualization — acknowledge this.

## OUTPUT — return valid JSON only
{{
  "discrepancies": [
    "On review, there appears to be ... — not described in the [date] report by [institution]. The reporting radiologist had full PACS/measurement tools this analysis did not. [Tier C]"
  ],
  "post_surgical_assessment": "Reconciliation of imaging with the operative note: ... (or null if no surgical notes)",
  "reconciliation_notes": "Brief summary of agreement/disagreement with priors."
}}
"""


# ═══════════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════════

@dataclass
class InterpretationRequest:
    """Package of data sent to Claude for interpretation."""
    measurements_json: dict
    key_images_b64: dict = field(default_factory=dict)  # label -> base64 PNG (legacy 4-image path)
    image_content_blocks: list = field(default_factory=list)  # NEW: from BatchSender (all images)
    prior_reports: Optional[str] = None
    surgical_notes: Optional[str] = None
    clinical_history: Optional[str] = None
    anatomy_type: str = "unknown"  # spine, brain, msk, unknown
    modality: str = "MR"           # DICOM Modality code (MR, CT, CR/DX, US, ...) for prompt discipline


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
    discrepancies: list = field(default_factory=list)  # vs prior reports (Phase 5)
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

    def __init__(self, api_key: str = "", model: str = "claude-opus-4-8", auth_token: str = ""):
        self.api_key = api_key
        self.auth_token = auth_token
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = build_anthropic_client(self.api_key, self.auth_token)
        return self._client

    def interpret(self, request: InterpretationRequest) -> ClinicalInterpretation:
        """
        Send measurements and images to Claude for clinical interpretation.
        Automatically selects the appropriate prompt template for the anatomy type.
        """
        anatomy = request.anatomy_type or "unknown"
        system_prompt = get_system_prompt(anatomy, getattr(request, "modality", "MR"))

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

        # 2. Images — use BatchSender content blocks if available, else legacy 4-image dict
        if request.image_content_blocks:
            # NEW PATH: All images from BatchSender (20-80 images)
            content_blocks.extend(request.image_content_blocks)
            logger.info(f"  Using BatchSender: {len(request.image_content_blocks)} content blocks")
        else:
            # LEGACY PATH: 4-image dict (backward compatible)
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
        n_images = len(request.image_content_blocks) if request.image_content_blocks else len(request.key_images_b64)
        logger.info(f"Sending {anatomy} interpretation request to {self.model}")
        logger.info(f"  Image blocks: {n_images}")
        logger.info(f"  Measurements keys: {list(request.measurements_json.keys())}")

        # More images = more findings = need more output tokens
        max_output_tokens = 16000 if request.image_content_blocks else 8000

        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_output_tokens,
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
            interpretation.discrepancies = parsed.get("discrepancies", [])
            # Normalize impression to a list[str] at the parse boundary so a model that returns a
            # string or object can't surface as one '[object Object]' bullet downstream. (BACKEND-4/DC-04)
            _imp = parsed.get("impression", [])
            if isinstance(_imp, list):
                interpretation.impression = [str(x) for x in _imp if str(x).strip()]
            elif isinstance(_imp, str):
                interpretation.impression = [_imp] if _imp.strip() else []
            elif _imp:
                interpretation.impression = [str(_imp)]
            else:
                interpretation.impression = []
            interpretation.confidence_summary = parsed.get("confidence_summary", {})

        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse JSON from Claude response: {e}")
            interpretation.impression = [raw_text]

        return interpretation

    def reconcile(
        self,
        blind_report: dict,
        image_content_blocks: list,
        surgical_notes: Optional[str] = None,
        prior_reports: Optional[str] = None,
        anatomy_type: str = "unknown",
    ) -> dict:
        """
        Phase 5 — surgical/prior-report reconciliation, run AFTER the blind read.
        Returns {"discrepancies": [...], "post_surgical_assessment": str|None,
        "reconciliation_notes": str, "input_tokens": int, "output_tokens": int}.
        """
        content_blocks = list(image_content_blocks)
        content_blocks.append({
            "type": "text",
            "text": RECONCILIATION_PROMPT.format(
                blind_report=json.dumps(blind_report, indent=2)
            ),
        })
        if surgical_notes:
            content_blocks.append({
                "type": "text",
                "text": f"\n## Surgical / Operative Notes\n{surgical_notes}",
            })
        if prior_reports:
            content_blocks.append({
                "type": "text",
                "text": f"\n## Prior Radiology Reports\n{prior_reports}",
            })

        result = {
            "discrepancies": [], "post_surgical_assessment": None,
            "reconciliation_notes": "", "input_tokens": 0, "output_tokens": 0,
        }
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=(
                    "You are a board-certified radiologist performing post-read "
                    "reconciliation of an independent blind read against prior reports "
                    "and operative notes. Preserve the blind read; flag differences with "
                    "appropriately qualified language."
                ),
                messages=[{"role": "user", "content": content_blocks}],
            )
            raw_text = response.content[0].text
            result["input_tokens"] = response.usage.input_tokens
            result["output_tokens"] = response.usage.output_tokens

            json_str = raw_text
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]
            parsed = json.loads(json_str.strip())
            result["discrepancies"] = parsed.get("discrepancies", [])
            result["post_surgical_assessment"] = parsed.get("post_surgical_assessment")
            result["reconciliation_notes"] = parsed.get("reconciliation_notes", "")
        except Exception as e:
            logger.warning(f"Reconciliation pass failed (keeping blind read): {e}")

        return result

    def interpret_streaming(self, request: InterpretationRequest):
        """
        Streaming version — yields partial text chunks for real-time UI updates.
        """
        anatomy = request.anatomy_type or "unknown"
        system_prompt = get_system_prompt(anatomy, getattr(request, "modality", "MR"))

        # Include the calibration status so this path can never become an
        # mm-fabrication hole if it is ever wired up (matches interpret()).
        calibration = request.measurements_json.get("calibration_status", "UNCALIBRATED")
        content_blocks = []
        content_blocks.append({
            "type": "text",
            "text": (
                f"## Pre-Computed Measurements (Calibration: {calibration})\n\n"
                f"```json\n{json.dumps(request.measurements_json, indent=2)}\n```\n\n"
                "If calibration is UNCALIBRATED, state no specific mm value; use qualitative "
                "language plus \"(visual estimate — no calibrated measurement available)\"."
            ),
        })

        # Prefer the full BatchSender image set when present; fall back to key images.
        if request.image_content_blocks:
            content_blocks.extend(request.image_content_blocks)
        else:
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
