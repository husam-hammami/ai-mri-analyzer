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
    quality_score: int = 0
    quality_notes: str = ""
    raw_response: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


# ── Verification Prompt ──

VERIFICATION_PROMPT = """
## ROLE
You are a senior attending radiologist with 20+ years of experience at a major
academic medical center. A junior colleague has produced the initial report below.
Your job is to VERIFY every finding against the actual images you are now reviewing.

You have access to the SAME images that the junior radiologist reviewed.

## THE INITIAL REPORT TO VERIFY
```json
{initial_report}
```

## THE ORIGINAL MEASUREMENTS DATA
```json
{measurements}
```

## YOUR REVIEW PROTOCOL — CHECK EVERY ITEM

### A. VERIFY EACH EXISTING FINDING
For EACH finding in the initial report:
1. Can you see this finding in the images? (confirmed / not seen / uncertain)
2. Is the grading/classification correct? (correct / should upgrade / should downgrade)
3. Is the anatomical location correct? (correct / wrong location)
4. Is the confidence tier appropriate? (correct / too high / too low)
5. Are the measurements consistent with what you see? (consistent / inconsistent)

### B. CHECK FOR MISSED FINDINGS
6. Are there any abnormalities visible in the images that are NOT in the report?
7. Are all anatomical regions covered by the systematic checklist?
8. Are incidental findings noted?

### C. QUALITY CONTROL
9. Does the impression accurately summarize the key findings?
10. Are findings ordered by clinical significance in the impression?
11. Is the language precise and unambiguous?
12. Are confidence tiers correctly applied per the framework rules?

### D. MEASUREMENT CROSS-CHECK
13. Do measurement-based findings match the provided measurements data?
14. Are uncalibrated measurements properly capped at Tier C?
15. Are any measurements fabricated (not in the data but reported)?

## CRITICAL RULES FOR VERIFICATION
- If you CANNOT see a reported finding in the images, DOWNGRADE or REMOVE it.
- If you see something the initial report MISSED, ADD it with proper tier.
- If you see a grading error, CORRECT it with explanation.
- Do NOT add findings you cannot see — that would make things worse.
- When in doubt, the LESS severe interpretation wins.
- The initial report's structure and format should be preserved.

## OUTPUT FORMAT
Return valid JSON:
{{
    "verified_findings": {{ ... corrected version of the full findings ... }},
    "corrections": [
        {{
            "finding": "L4-L5 disc herniation",
            "action": "downgraded",
            "reason": "Signal appears more consistent with Pfirrmann II than III on review"
        }},
        {{
            "finding": "L3-L4 foraminal stenosis left",
            "action": "removed",
            "reason": "Cannot confirm on available axial images — perineural fat appears preserved"
        }}
    ],
    "missed_findings": [
        {{
            "finding": "T12 mild compression fracture",
            "tier": "B",
            "reason": "Approximately 20% anterior height loss visible on sagittal T1, STIR shows no edema suggesting chronic"
        }}
    ],
    "quality_score": 82,
    "quality_notes": "Good systematic coverage. Two findings overcalled, one compression fracture missed. Impression appropriately prioritized."
}}

IMPORTANT: The "verified_findings" must have the SAME JSON structure as the input
findings. Preserve all field names. Update values where corrections are needed.
"""


class VerificationPass:
    """
    Run a verification pass on an initial Claude interpretation.
    Sends the initial report + all images to Claude as a senior attending.
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
                raise RuntimeError("anthropic SDK required")
        return self._client

    def verify(
        self,
        initial_report: dict,
        image_content_blocks: list[dict],
        measurements_json: dict,
        anatomy_type: str,
    ) -> VerifiedReport:
        """
        Send initial report + images to Claude for senior attending review.

        Args:
            initial_report: The raw JSON from the first Claude interpretation
            image_content_blocks: Same image blocks sent to the first call
            measurements_json: Same measurements data
            anatomy_type: The detected anatomy type

        Returns:
            VerifiedReport with corrections, missed findings, and quality score
        """
        # Build the verification prompt with the initial report embedded
        verification_text = VERIFICATION_PROMPT.format(
            initial_report=json.dumps(initial_report, indent=2),
            measurements=json.dumps(measurements_json, indent=2),
        )

        # Build content blocks: images first, then verification prompt
        content_blocks = []

        # Add all images (same as first pass)
        content_blocks.extend(image_content_blocks)

        # Add the verification task
        content_blocks.append({
            "type": "text",
            "text": verification_text,
        })

        logger.info(
            f"Running verification pass for {anatomy_type} "
            f"({len(image_content_blocks)} image blocks)"
        )

        # Make the API call
        response = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=(
                "You are a senior attending radiologist performing quality assurance "
                "review of a junior colleague's MRI interpretation. Be thorough but fair. "
                "Only correct genuine errors — do not make changes for the sake of change."
            ),
            messages=[{
                "role": "user",
                "content": content_blocks,
            }],
        )

        raw_text = response.content[0].text
        logger.info(
            f"Verification response: {response.usage.input_tokens} input, "
            f"{response.usage.output_tokens} output tokens"
        )

        # Parse the verification result
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
            result.quality_score = parsed.get("quality_score", 0)
            result.quality_notes = parsed.get("quality_notes", "")

        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse verification JSON: {e}")
            # Fall back to using the initial report unchanged
            result.verified_findings = initial_report
            result.quality_notes = f"Verification parsing failed: {e}"
            result.quality_score = 50

        return result
