"""
BatchSender — Send ALL MRI images to Claude organized by diagnostic priority.
==============================================================================
Replaces the 4-image bottleneck in app.py. Collects all converted PNG images
from work_dir/raw_png/, prioritizes by anatomy-specific diagnostic value,
encodes as JPEG within token budget, and returns Claude content blocks.

This is Module 1 of Plan C+V — the single biggest accuracy improvement.
"""

import io
import base64
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

from PIL import Image

logger = logging.getLogger("mika.batch_sender")


# ── Configuration ──

MAX_IMAGES = 80           # Claude vision limit per request
TARGET_TOKENS = 150_000   # Leave room for prompt (~30K) + response (~8K)
JPEG_QUALITY = 80         # Balance quality vs token cost
MAX_IMAGE_DIM = 1024      # Resize images larger than this (pixels)
TOKENS_PER_IMAGE = 1600   # ~1600 tokens per 1024px JPEG image (estimate)


# ── Data Models ──

@dataclass
class ImageEntry:
    """A single image ready to be sent to Claude."""
    path: Path
    sequence_name: str
    slice_num: int
    total_slices: int
    plane: str = ""
    priority: int = 99  # Lower = higher priority


# ── Anatomy-Specific Priority Tables ──
# Lower number = sent first (most diagnostically important)

PRIORITY_ORDER = {
    "spine": [
        "t2_sag", "t2_tse_sag",                       # 1: Primary — disc, CSF, cord
        "t1_sag", "t1_tse_sag",                       # 2: Marrow, endplates
        "tirm_sag", "stir_sag", "flair_sag",          # 3: Edema, Modic 1
        "t2_ax", "t2_tse_tra", "t2_tra",              # 4: Canal cross-section, foramina
        "t1_ax", "t1_tse_tra", "t1_tra",              # 5: Foraminal fat
        "t1_cont", "t1_post", "t1_gd",                # 6: Enhancement
        "vibe", "t2_cor",                              # 7: Additional
    ],
    "brain": [
        "flair", "dark_fluid",                         # 1: WM lesions
        "dwi", "diffusion", "ep2d_diff",               # 2: Acute pathology
        "t1_cont", "t1_post", "t1_gd", "t1_mprage_post",  # 3: Enhancement
        "t2", "t2_tse",                                # 4: Structural
        "swi", "suscept", "gre",                       # 5: Blood, calcium
        "adc",                                         # 6: ADC map
        "t1", "t1_mprage", "t1_tse",                   # 7: Anatomy
        "tof", "mra",                                  # 8: Vascular
    ],
    "msk": [
        "pd_fs", "pd_fat",                             # 1: Fluid-sensitive
        "t2_fs", "t2_fat", "stir",                     # 2: Edema, tears
        "pd", "t2",                                    # 3: Structural
        "t1",                                          # 4: Anatomy, marrow
        "t1_cont", "t1_post",                          # 5: Enhancement
        "t2_star", "gre",                              # 6: Susceptibility
    ],
    "cardiac": [
        "cine", "ssfp", "trufi", "bssfp",             # 1: Function
        "lge", "late_gad", "psir",                     # 2: Scar/fibrosis
        "t2_stir", "t2_edema",                         # 3: Edema
        "t1_map", "molli",                             # 4: T1 mapping
        "t2_map",                                      # 5: T2 mapping
        "perf", "perfusion",                           # 6: Perfusion
        "flow", "phase_contrast",                      # 7: Flow
    ],
    "chest": [
        "t2_haste", "haste",                           # 1: Lung parenchyma
        "t1_cont", "vibe_post",                        # 2: Enhancement
        "dwi", "diffusion",                            # 3: Cellularity
        "stir", "tirm",                                # 4: Edema
        "t1", "vibe_pre",                              # 5: Anatomy
    ],
    "abdomen": [
        "t2_fs", "t2_haste",                           # 1: Fluid, lesions
        "dwi", "diffusion",                            # 2: Cellularity
        "t1_portal", "t1_cont_portal", "vibe_portal",  # 3: Portal phase
        "t1_art", "t1_cont_art", "vibe_art",           # 4: Arterial phase
        "t1_delayed", "vibe_delayed",                  # 5: Delayed
        "t1_opp", "t1_opposed", "dixon_opp",           # 6: Chemical shift
        "t1_in", "t1_inphase", "dixon_in",             # 7: In-phase
        "t1_pre", "vibe_pre",                          # 8: Pre-contrast
        "mrcp", "t2_cor",                              # 9: Biliary
    ],
    "breast": [
        "t1_sub", "sub", "subtraction",                # 1: Subtraction (enhancement)
        "t1_cont", "t1_post", "t1_dyn",               # 2: Post-contrast dynamic
        "dwi", "diffusion",                            # 3: Cellularity
        "t2",                                          # 4: Cyst vs solid
        "stir", "tirm",                                # 5: Edema
        "t1_pre",                                      # 6: Pre-contrast baseline
        "mip",                                         # 7: MIP projection
    ],
    "vascular": [
        "tof", "mra", "ce_mra",                       # 1: Angiography
        "t1_bb", "black_blood",                        # 2: Vessel wall
        "phase", "flow",                               # 3: Flow quantification
        "t2", "t1",                                    # 4: Anatomy
    ],
    "head_neck": [
        "t1_cont_fs", "t1_post_fs", "t1_gd_fs",       # 1: Enhancement + fat sat
        "t2_fs", "t2_fat",                             # 2: Edema, pathology
        "dwi", "diffusion",                            # 3: Cellularity
        "t1", "t1_tse",                                # 4: Anatomy
        "t2", "t2_tse",                                # 5: Structure
        "stir", "tirm",                                # 6: Fat-suppressed fluid
    ],
    "prostate": [
        "dwi", "diffusion", "ep2d_diff",               # 1: PI-RADS dominant (PZ)
        "adc",                                         # 2: ADC map
        "t2", "t2_tse",                                # 3: PI-RADS dominant (TZ)
        "t1_dce", "t1_cont", "dce", "perf",            # 4: DCE
        "t1", "t1_tse",                                # 5: Pre-contrast anatomy
    ],
}


class BatchSender:
    """
    Collect, prioritize, and encode ALL MRI images for Claude.

    Replaces the 4-image hardcoded block in app.py with a system that
    sends 20-80 images organized by diagnostic importance.
    """

    def __init__(
        self,
        work_dir: Path,
        anatomy_type: str,
        max_images: int = MAX_IMAGES,
        target_tokens: int = TARGET_TOKENS,
    ):
        self.work_dir = Path(work_dir)
        self.anatomy = anatomy_type
        self.max_images = max_images
        self.target_tokens = target_tokens
        self.raw_png_dir = self.work_dir / "raw_png"

    def collect_all_images(self) -> list[ImageEntry]:
        """Scan raw_png/ directory for all converted slices.

        Expected structure: raw_png/{sequence_name}/slice_001.png
        """
        images = []

        if not self.raw_png_dir.exists():
            logger.warning(f"raw_png directory not found: {self.raw_png_dir}")
            return images

        for seq_dir in sorted(self.raw_png_dir.iterdir()):
            if not seq_dir.is_dir():
                continue

            seq_name = seq_dir.name
            slice_files = sorted(seq_dir.glob("*.png"))

            if not slice_files:
                continue

            total = len(slice_files)
            for i, slice_path in enumerate(slice_files):
                images.append(ImageEntry(
                    path=slice_path,
                    sequence_name=seq_name,
                    slice_num=i + 1,
                    total_slices=total,
                    plane=self._guess_plane(seq_name),
                    priority=self._get_priority(seq_name),
                ))

        logger.info(f"Collected {len(images)} images from {self.raw_png_dir}")
        return images

    def prioritize(self, images: list[ImageEntry]) -> list[ImageEntry]:
        """Sort by diagnostic priority and apply smart slice selection.

        For sequences with many slices, we use strategic sampling:
        - Always include the middle slice (most anatomy)
        - Include slices at 25% and 75% positions
        - Fill remaining budget evenly
        """
        if not images:
            return []

        # Group by sequence
        seq_groups: dict[str, list[ImageEntry]] = {}
        for img in images:
            key = img.sequence_name
            if key not in seq_groups:
                seq_groups[key] = []
            seq_groups[key].append(img)

        # Sort sequences by priority
        sorted_seqs = sorted(seq_groups.keys(), key=lambda s: self._get_priority(s))

        # Budget allocation: higher priority sequences get more images
        total_budget = min(self.max_images, self.target_tokens // TOKENS_PER_IMAGE)
        num_seqs = len(sorted_seqs)

        # Allocate: top sequences get more slices
        allocations = {}
        remaining = total_budget
        for i, seq_name in enumerate(sorted_seqs):
            available = len(seq_groups[seq_name])
            if i < 3:
                # Top 3 sequences: up to 40% of budget shared
                alloc = min(available, max(3, remaining // max(1, (num_seqs - i))))
            elif i < 6:
                # Next 3: moderate allocation
                alloc = min(available, max(2, remaining // max(1, (num_seqs - i))))
            else:
                # Rest: minimum coverage
                alloc = min(available, max(1, remaining // max(1, (num_seqs - i))))

            allocations[seq_name] = alloc
            remaining -= alloc
            if remaining <= 0:
                break

        # Select slices using strategic sampling
        selected = []
        for seq_name in sorted_seqs:
            if seq_name not in allocations:
                continue

            group = seq_groups[seq_name]
            n_alloc = allocations[seq_name]

            if n_alloc >= len(group):
                # Send all slices
                selected.extend(group)
            else:
                # Strategic sampling
                indices = self._strategic_slice_indices(len(group), n_alloc)
                for idx in indices:
                    selected.append(group[idx])

        logger.info(
            f"Selected {len(selected)} images from {len(seq_groups)} sequences "
            f"(budget: {total_budget}, anatomy: {self.anatomy})"
        )
        return selected

    def encode_batch(self, images: list[ImageEntry]) -> list[dict]:
        """Encode images as JPEG base64 with diagnostic labels.

        Returns list of Claude API content blocks ready to send.
        Each image is preceded by a text label with context.
        """
        content_blocks = []
        current_seq = None

        for img in images:
            # Add sequence header when sequence changes
            if img.sequence_name != current_seq:
                current_seq = img.sequence_name
                seq_count = sum(1 for i in images if i.sequence_name == current_seq)
                plane_label = img.plane.upper() if img.plane else "UNKNOWN"
                content_blocks.append({
                    "type": "text",
                    "text": f"\n=== {current_seq.upper()} ({plane_label}) — {seq_count} slices ===\n",
                })

            # Encode image as JPEG
            try:
                b64_data = self._encode_image(img.path)
            except Exception as e:
                logger.warning(f"Failed to encode {img.path}: {e}")
                continue

            # Label for this slice
            content_blocks.append({
                "type": "text",
                "text": f"[Slice {img.slice_num}/{img.total_slices}]",
            })

            # Image block
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64_data,
                },
            })

        return content_blocks

    def build_message_content(self) -> tuple[list[dict], int]:
        """Full pipeline: collect -> prioritize -> encode.

        Returns:
            Tuple of (content_blocks, image_count)
        """
        images = self.collect_all_images()
        if not images:
            logger.warning("No images found — returning empty content")
            return [], 0

        selected = self.prioritize(images)
        content_blocks = self.encode_batch(selected)

        return content_blocks, len(selected)

    # ── Private Methods ──

    def _get_priority(self, seq_name: str) -> int:
        """Get priority score for a sequence name. Lower = higher priority."""
        seq_lower = seq_name.lower()
        priority_list = PRIORITY_ORDER.get(self.anatomy, [])

        for i, keyword in enumerate(priority_list):
            if keyword in seq_lower or seq_lower in keyword:
                return i

        # Check partial matches
        for i, keyword in enumerate(priority_list):
            if any(part in seq_lower for part in keyword.split("_") if len(part) > 2):
                return i + 50  # Partial match, lower priority

        return 99  # Unknown sequence

    def _guess_plane(self, seq_name: str) -> str:
        """Guess imaging plane from sequence name."""
        name = seq_name.lower()
        if any(k in name for k in ["sag", "sagittal"]):
            return "sagittal"
        if any(k in name for k in ["ax", "tra", "axial", "transverse"]):
            return "axial"
        if any(k in name for k in ["cor", "coronal"]):
            return "coronal"
        return ""

    def _strategic_slice_indices(self, total: int, n_select: int) -> list[int]:
        """Select strategically important slice indices.

        Always includes: middle, 25%, 75%.
        Fills remaining budget with evenly spaced slices.
        """
        if n_select >= total:
            return list(range(total))

        # Key positions
        key_positions = set()
        key_positions.add(total // 2)          # Middle
        if n_select >= 3:
            key_positions.add(total // 4)      # 25%
            key_positions.add(3 * total // 4)  # 75%
        if n_select >= 5:
            key_positions.add(total // 8)      # 12.5%
            key_positions.add(7 * total // 8)  # 87.5%

        # Fill remaining with even spacing
        remaining = n_select - len(key_positions)
        if remaining > 0:
            step = total / (remaining + 1)
            for i in range(1, remaining + 1):
                idx = int(i * step)
                if idx < total:
                    key_positions.add(idx)

        # Sort and return
        indices = sorted(list(key_positions))[:n_select]
        return indices

    def _encode_image(self, path: Path) -> str:
        """Load PNG, resize if needed, encode as JPEG base64."""
        img = Image.open(path)

        # Convert to RGB if needed (JPEG doesn't support RGBA)
        if img.mode in ("RGBA", "P", "L", "LA"):
            if img.mode == "L" or img.mode == "LA":
                # Grayscale — convert to RGB for JPEG
                img = img.convert("RGB")
            else:
                img = img.convert("RGB")

        # Resize if too large
        w, h = img.size
        if max(w, h) > MAX_IMAGE_DIM:
            ratio = MAX_IMAGE_DIM / max(w, h)
            new_w = int(w * ratio)
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # Encode to JPEG
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
