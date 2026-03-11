"""
MIKA Master Prompts — Fellowship-Level Radiology Prompt Library
================================================================
Each anatomy type has a dedicated master prompt with:
  - Mandatory systematic search checklist
  - Grading criteria tables (Pfirrmann, BI-RADS, PI-RADS, etc.)
  - Normal measurement references
  - Sequence interpretation guide
  - Anti-hallucination rules
  - Structured JSON output schema

Usage:
    from backend.prompts import get_master_prompt
    prompt = get_master_prompt("spine")
"""

try:
    from backend.prompts.base_prompt import BASE_RULES
    from backend.prompts.spine_master import SPINE_MASTER_PROMPT
    from backend.prompts.brain_master import BRAIN_MASTER_PROMPT
    from backend.prompts.msk_master import MSK_MASTER_PROMPT
    from backend.prompts.cardiac_master import CARDIAC_MASTER_PROMPT
    from backend.prompts.chest_master import CHEST_MASTER_PROMPT
    from backend.prompts.abdomen_master import ABDOMEN_MASTER_PROMPT
    from backend.prompts.breast_master import BREAST_MASTER_PROMPT
    from backend.prompts.vascular_master import VASCULAR_MASTER_PROMPT
    from backend.prompts.head_neck_master import HEAD_NECK_MASTER_PROMPT
    from backend.prompts.prostate_master import PROSTATE_MASTER_PROMPT
except ImportError:
    from prompts.base_prompt import BASE_RULES
    from prompts.spine_master import SPINE_MASTER_PROMPT
    from prompts.brain_master import BRAIN_MASTER_PROMPT
    from prompts.msk_master import MSK_MASTER_PROMPT
    from prompts.cardiac_master import CARDIAC_MASTER_PROMPT
    from prompts.chest_master import CHEST_MASTER_PROMPT
    from prompts.abdomen_master import ABDOMEN_MASTER_PROMPT
    from prompts.breast_master import BREAST_MASTER_PROMPT
    from prompts.vascular_master import VASCULAR_MASTER_PROMPT
    from prompts.head_neck_master import HEAD_NECK_MASTER_PROMPT
    from prompts.prostate_master import PROSTATE_MASTER_PROMPT

PROMPT_MAP = {
    "spine": SPINE_MASTER_PROMPT,
    "brain": BRAIN_MASTER_PROMPT,
    "msk": MSK_MASTER_PROMPT,
    "cardiac": CARDIAC_MASTER_PROMPT,
    "chest": CHEST_MASTER_PROMPT,
    "abdomen": ABDOMEN_MASTER_PROMPT,
    "breast": BREAST_MASTER_PROMPT,
    "vascular": VASCULAR_MASTER_PROMPT,
    "head_neck": HEAD_NECK_MASTER_PROMPT,
    "prostate": PROSTATE_MASTER_PROMPT,
}


def get_master_prompt(anatomy_type: str) -> str:
    """Get the master prompt for a specific anatomy type.
    Falls back to a generic prompt if anatomy is unknown."""
    return PROMPT_MAP.get(anatomy_type, _GENERIC_MASTER_PROMPT)


_GENERIC_MASTER_PROMPT = BASE_RULES + """
## UNKNOWN ANATOMY — ADAPTIVE INTERPRETATION

The body region could not be automatically determined from DICOM metadata.

### YOUR FIRST TASK
Identify the anatomy being studied from the images. Then apply the appropriate
systematic assessment for that anatomy.

### OUTPUT JSON SCHEMA
{
  "identified_anatomy": "description of anatomy and body region",
  "findings_by_region": {
    "region_1": "detailed findings...",
    "region_2": "detailed findings..."
  },
  "incidentals": [],
  "impression": ["1. ...", "2. ..."],
  "confidence_summary": {
    "tier_a": [],
    "tier_b": [],
    "tier_c": [],
    "tier_d": ["Anatomy type could not be auto-detected"]
  }
}
"""
