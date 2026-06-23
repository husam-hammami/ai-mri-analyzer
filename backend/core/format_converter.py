"""
MIKA — Multi-Format Input Converter
========================================
Converts various medical imaging formats into MIKA's internal working format.
Supports: DICOM (.dcm), NIfTI (.nii, .nii.gz), NRRD (.nrrd),
          standard images (PNG, JPG, TIFF), and ZIP archives containing any of these.

The converter normalizes all inputs into a common structure:
  - PNG slices in a working directory
  - A metadata JSON file with whatever calibration/demographics data is available
  - Synthetic .dcm-compatible inventory for the DICOMEngine to process

Architecture:
  1. Detect input format from file extensions
  2. Convert to PNG slices + metadata
  3. Optionally create lightweight synthetic DICOM wrappers for the engine
"""

import os
import io
import re
import json
import shutil
import zipfile
import logging
import tempfile
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

import numpy as np
from PIL import Image

logger = logging.getLogger("mika.format")

# ── Optional dependency imports ──

try:
    import nibabel as nib
    NIBABEL_AVAILABLE = True
except ImportError:
    NIBABEL_AVAILABLE = False

try:
    import nrrd as nrrd_lib
    NRRD_AVAILABLE = True
except ImportError:
    NRRD_AVAILABLE = False

try:
    import pydicom
    from pydicom.dataset import Dataset, FileDataset
    from pydicom.uid import ExplicitVRLittleEndian
    from pydicom.sequence import Sequence
    PYDICOM_AVAILABLE = True
except ImportError:
    PYDICOM_AVAILABLE = False


# ── Supported Formats ──

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
NIFTI_EXTENSIONS = {".nii", ".nii.gz", ".gz"}  # .gz handled specially
NRRD_EXTENSIONS = {".nrrd", ".nhdr"}
DICOM_EXTENSIONS = {".dcm", ".ima", ".dicom"}
ARCHIVE_EXTENSIONS = {".zip"}


@dataclass
class ConversionResult:
    """Result of converting any input format to MIKA's internal format."""
    success: bool
    input_format: str                          # "dicom", "nifti", "nrrd", "images", "zip"
    dicom_dir: str                             # Directory with .dcm files (real or synthetic)
    num_files: int                             # Total files produced
    num_slices: int                            # Total slices (across all sequences)
    sequences_detected: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)  # Any extracted metadata
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None


def choose_slice_axis(shape, spacing) -> tuple[int, Optional[str]]:
    """Pick the through-plane (slice) axis of a 3-D volume.

    The through-plane axis is the one with the LARGEST voxel spacing — slices are
    spaced farther apart than in-plane pixels. This is the fix for the .mha/.nii
    transpose bug: a ~50-slice sagittal study was being iterated along its 578-row
    in-plane axis, producing 578 thin "slices" and mislabelling every disc level.

    ``shape`` and ``spacing`` are 3-tuples aligned to the SAME axis order (i.e.
    spacing[i] is the voxel size along the array's axis i). Returns ``(axis,
    warning)`` where ``warning`` is ``None`` on a confident pick or a
    human-readable string when the pick is low-confidence or looks transposed.

    - Anisotropic voxels → ``argmax(spacing)`` (the confident case).
    - Near-isotropic voxels → fall back to the axis with the FEWEST samples
      (``argmin(shape)``) and flag low confidence, because spacing can no longer
      name the slice axis.
    - 578-signature guard: if the chosen axis still has hundreds of very thin
      slices it is almost certainly a transpose — warn loudly.
    """
    shape = tuple(int(s) for s in shape)
    spacing = tuple(float(s) for s in spacing)
    if len(shape) != 3 or len(spacing) != 3:
        raise ValueError(
            f"choose_slice_axis expects 3-D shape and spacing, got shape={shape!r} spacing={spacing!r}"
        )

    warning: Optional[str] = None
    sp_max = max(spacing)
    sp_min = min(spacing)
    # Near-isotropic: the largest spacing is barely larger than the smallest, so
    # spacing can't reliably distinguish through-plane from in-plane. Most volumes
    # carry the fewest samples through-plane, so fall back to argmin(shape).
    near_isotropic = sp_max <= 0 or (sp_min > 0 and (sp_max / sp_min) < 1.5)
    if near_isotropic:
        axis = int(np.argmin(shape))
        warning = (
            f"near-isotropic voxels {spacing} — slice axis inferred from the smallest "
            f"dimension (axis {axis}, {shape[axis]} samples); low confidence"
        )
    else:
        axis = int(np.argmax(spacing))

    # 578-signature guard: a through-plane axis with hundreds of sub-2mm slices is
    # the transpose symptom (a ~50-slice study read as 578 thin slices). Warn loudly.
    if shape[axis] > 400 and spacing[axis] < 2.0:
        warning = (
            f"suspicious slice axis {axis}: {shape[axis]} slices at {spacing[axis]:.2f} mm — "
            f"looks like a transposed volume (shape={shape}, spacing={spacing}); verify orientation"
        )
    return axis, warning


class FormatConverter:
    """
    Converts any supported medical imaging format into DICOM files
    that the DICOMEngine can process natively.
    """

    def __init__(self, input_dir: str, output_dir: str):
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def detect_format(self) -> str:
        """
        Detect the primary format of files in the input directory.
        Returns: 'dicom', 'nifti', 'nrrd', 'images', 'zip', or 'unknown'
        """
        files = list(self.input_dir.iterdir()) if self.input_dir.is_dir() else []
        if not files:
            return "unknown"

        ext_counts = {}
        for f in files:
            if f.is_file():
                ext = f.suffix.lower()
                # Handle .nii.gz (double extension)
                if ext == ".gz" and f.stem.endswith(".nii"):
                    ext = ".nii.gz"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1

        # Check archives first
        for ext in ARCHIVE_EXTENSIONS:
            if ext in ext_counts:
                return "zip"

        # Check DICOM
        for ext in DICOM_EXTENSIONS:
            if ext in ext_counts:
                return "dicom"

        # Also check for DICOM files without extension (common in PACS exports)
        for f in files:
            if f.is_file() and f.suffix == "":
                try:
                    if PYDICOM_AVAILABLE:
                        pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
                        return "dicom"
                except Exception:
                    pass
                break  # Only test first extensionless file

        # Check NIfTI
        if ".nii" in ext_counts or ".nii.gz" in ext_counts:
            return "nifti"

        # Check NRRD
        for ext in NRRD_EXTENSIONS:
            if ext in ext_counts:
                return "nrrd"

        # Check images
        for ext in IMAGE_EXTENSIONS:
            if ext in ext_counts:
                return "images"

        return "unknown"

    def convert(self) -> ConversionResult:
        """
        Auto-detect format and convert to DICOM-compatible directory.
        Returns ConversionResult with the path to the output directory.
        """
        fmt = self.detect_format()
        logger.info(f"Detected input format: {fmt} in {self.input_dir}")

        if fmt == "dicom":
            return self._handle_dicom()
        elif fmt == "nifti":
            return self._convert_nifti()
        elif fmt == "nrrd":
            return self._convert_nrrd()
        elif fmt == "images":
            return self._convert_images()
        elif fmt == "zip":
            return self._handle_zip()
        else:
            return ConversionResult(
                success=False,
                input_format="unknown",
                dicom_dir=str(self.output_dir),
                num_files=0,
                num_slices=0,
                error="No supported files found. Supported formats: DICOM (.dcm), NIfTI (.nii/.nii.gz), NRRD (.nrrd), images (PNG/JPG/TIFF), or ZIP archives.",
            )

    # ── DICOM (passthrough) ──

    def _handle_dicom(self) -> ConversionResult:
        """DICOM files — just ensure they're in the output dir with .dcm extension."""
        count = 0
        for f in sorted(self.input_dir.iterdir()):
            if f.is_file():
                ext = f.suffix.lower()
                if ext in DICOM_EXTENSIONS:
                    dest = self.output_dir / f.name
                    if self.input_dir != self.output_dir:
                        shutil.copy2(str(f), str(dest))
                    count += 1
                elif ext == "" and PYDICOM_AVAILABLE:
                    # Extensionless DICOM — copy with .dcm extension
                    try:
                        pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
                        dest = self.output_dir / f"{f.name}.dcm"
                        shutil.copy2(str(f), str(dest))
                        count += 1
                    except Exception:
                        pass

        return ConversionResult(
            success=count > 0,
            input_format="dicom",
            dicom_dir=str(self.output_dir),
            num_files=count,
            num_slices=count,
        )

    # ── NIfTI ──

    def _convert_nifti(self) -> ConversionResult:
        """Convert NIfTI (.nii, .nii.gz) files to synthetic DICOM."""
        if not NIBABEL_AVAILABLE:
            return ConversionResult(
                success=False, input_format="nifti", dicom_dir=str(self.output_dir),
                num_files=0, num_slices=0,
                error="nibabel is required for NIfTI support. Install with: pip install nibabel",
            )

        nifti_files = []
        for f in sorted(self.input_dir.iterdir()):
            if f.is_file():
                name_lower = f.name.lower()
                if name_lower.endswith(".nii") or name_lower.endswith(".nii.gz"):
                    nifti_files.append(f)

        if not nifti_files:
            return ConversionResult(
                success=False, input_format="nifti", dicom_dir=str(self.output_dir),
                num_files=0, num_slices=0, error="No NIfTI files found",
            )

        total_slices = 0
        sequences = []
        warnings = []
        metadata = {}

        for nii_path in nifti_files:
            try:
                img = nib.load(str(nii_path))
                data = np.asanyarray(img.dataobj)
                header = img.header
                affine = img.affine

                # Per-axis voxel dimensions (mm). The slice axis is chosen below from
                # these (largest spacing = through-plane); we deliberately do NOT assume
                # axis 2 is through-plane — that assumption transposed sagittal studies
                # into hundreds of thin slices and mislabelled every disc level.
                voxel_dims = header.get_zooms()

                # Derive sequence name from filename
                seq_name = nii_path.stem
                if seq_name.endswith(".nii"):
                    seq_name = seq_name[:-4]
                sequences.append(seq_name)

                # Guess the imaging modality from the filename so CT/PET/US/X-ray
                # studies aren't hard-labeled MR. Fall back to MR but warn loudly.
                guessed_modality = self._guess_modality_from_name(nii_path.name)
                if guessed_modality is None:
                    modality = "MR"
                    warnings.append(
                        "Modality could not be determined from a NIfTI/NRRD import and was "
                        "assumed MR — if this is a CT/PET/ultrasound study the interpretation "
                        "may be mislabeled."
                    )
                else:
                    modality = guessed_modality

                # Handle 3D and 4D volumes
                if data.ndim == 4:
                    # 4D: use first volume
                    data = data[:, :, :, 0]
                    warnings.append(f"{nii_path.name}: 4D volume detected, using first timepoint")

                if data.ndim != 3:
                    warnings.append(f"{nii_path.name}: Expected 3D volume, got {data.ndim}D — skipping")
                    continue

                # Choose the through-plane axis from voxel spacing (largest = slice axis).
                # in-plane spacing/dims follow the chosen axis rather than fixed positions.
                axis_spacing = [
                    float(voxel_dims[i]) if i < len(voxel_dims) else 0.0 for i in range(3)
                ]
                slice_axis, axis_warning = choose_slice_axis(data.shape, axis_spacing)
                if axis_warning:
                    warnings.append(f"{nii_path.name}: {axis_warning}")
                in_plane = [a for a in range(3) if a != slice_axis]
                row_axis, col_axis = in_plane[0], in_plane[1]
                pixdim_row = axis_spacing[row_axis]
                pixdim_col = axis_spacing[col_axis]
                slice_thickness = axis_spacing[slice_axis] if axis_spacing[slice_axis] > 0 else 1.0
                # Only mark calibrated when both in-plane spacings are real — never fabricate
                # authoritative mm from a 1.0 fallback (the #1 failure mode the skill prevents).
                has_spacing = pixdim_row > 0 and pixdim_col > 0
                if not has_spacing:
                    pixdim_row = pixdim_row if pixdim_row > 0 else 1.0
                    pixdim_col = pixdim_col if pixdim_col > 0 else 1.0
                    warnings.append(
                        f"{nii_path.name}: no usable voxel spacing in header — "
                        "treating as UNCALIBRATED (no mm measurements will be reported)"
                    )

                # Normalize to 0-255
                d_min, d_max = float(data.min()), float(data.max())
                if d_max > d_min:
                    data_norm = ((data - d_min) / (d_max - d_min) * 255).astype(np.uint8)
                else:
                    data_norm = np.zeros_like(data, dtype=np.uint8)

                # Iterate TRUE through-plane slices (move the slice axis to the front).
                data_t = np.moveaxis(data_norm, slice_axis, 0)
                num_slices = data_t.shape[0]

                # Create synthetic DICOM for each slice
                for s in range(num_slices):
                    slice_data = data_t[s]
                    # Flip so orientation matches typical DICOM display
                    slice_data = np.flipud(slice_data)

                    dcm_filename = f"{seq_name}_Img{s+1:04d}.dcm"
                    self._create_synthetic_dicom(
                        pixel_data=slice_data,
                        output_path=self.output_dir / dcm_filename,
                        series_description=seq_name,
                        instance_number=s + 1,
                        slice_location=float(s * slice_thickness),
                        pixel_spacing=[pixdim_row, pixdim_col],
                        slice_thickness=slice_thickness,
                        rows=slice_data.shape[0],
                        cols=slice_data.shape[1],
                        study_description=f"NIfTI Import: {nii_path.name}",
                        is_calibrated=has_spacing,
                        modality=modality,
                    )
                    total_slices += 1

                metadata[seq_name] = {
                    "source": str(nii_path.name),
                    "dimensions": list(data.shape),
                    "voxel_size_mm": [pixdim_row, pixdim_col, slice_thickness],
                    "slice_axis": slice_axis,
                    "num_slices": num_slices,
                }

            except Exception as e:
                warnings.append(f"Error processing {nii_path.name}: {str(e)}")

        return ConversionResult(
            success=total_slices > 0,
            input_format="nifti",
            dicom_dir=str(self.output_dir),
            num_files=total_slices,
            num_slices=total_slices,
            sequences_detected=sequences,
            metadata=metadata,
            warnings=warnings,
        )

    # ── NRRD ──

    def _convert_nrrd(self) -> ConversionResult:
        """Convert NRRD (.nrrd, .nhdr) files to synthetic DICOM."""
        if not NRRD_AVAILABLE:
            return ConversionResult(
                success=False, input_format="nrrd", dicom_dir=str(self.output_dir),
                num_files=0, num_slices=0,
                error="pynrrd is required for NRRD support. Install with: pip install pynrrd",
            )

        nrrd_files = [
            f for f in sorted(self.input_dir.iterdir())
            if f.is_file() and f.suffix.lower() in NRRD_EXTENSIONS
        ]

        if not nrrd_files:
            return ConversionResult(
                success=False, input_format="nrrd", dicom_dir=str(self.output_dir),
                num_files=0, num_slices=0, error="No NRRD files found",
            )

        total_slices = 0
        sequences = []
        warnings = []
        metadata = {}

        for nrrd_path in nrrd_files:
            try:
                data, header = nrrd_lib.read(str(nrrd_path))
                seq_name = nrrd_path.stem
                sequences.append(seq_name)

                # Guess the imaging modality from the filename so CT/PET/US/X-ray
                # studies aren't hard-labeled MR. Fall back to MR but warn loudly.
                guessed_modality = self._guess_modality_from_name(nrrd_path.name)
                if guessed_modality is None:
                    modality = "MR"
                    warnings.append(
                        "Modality could not be determined from a NIfTI/NRRD import and was "
                        "assumed MR — if this is a CT/PET/ultrasound study the interpretation "
                        "may be mislabeled."
                    )
                else:
                    modality = guessed_modality

                # Per-axis spacing from the NRRD header — the norm of each space-direction
                # row, else the 'spacings' field. The slice axis is chosen below from these
                # (largest = through-plane); 0.0 marks a missing/unparseable axis. We do NOT
                # assume axis 2 is through-plane.
                axis_spacing = [0.0, 0.0, 0.0]
                space_directions = header.get("space directions", None)
                if space_directions is not None:
                    for i in range(3):
                        try:
                            axis_spacing[i] = float(np.linalg.norm(space_directions[i]))
                        except (IndexError, TypeError, ValueError):
                            axis_spacing[i] = 0.0
                else:
                    spacings = header.get("spacings", None)
                    if spacings is not None:
                        for i in range(min(3, len(spacings))):
                            try:
                                axis_spacing[i] = float(spacings[i])
                            except (TypeError, ValueError):
                                axis_spacing[i] = 0.0

                # Handle 3D+ data
                if data.ndim == 4:
                    data = data[:, :, :, 0]
                    warnings.append(f"{nrrd_path.name}: 4D volume, using first component")

                if data.ndim != 3:
                    warnings.append(f"{nrrd_path.name}: Expected 3D, got {data.ndim}D — skipping")
                    continue

                # Choose the through-plane axis from spacing (largest = slice axis);
                # in-plane spacing/dims follow the chosen axis rather than fixed positions.
                slice_axis, axis_warning = choose_slice_axis(data.shape, axis_spacing)
                if axis_warning:
                    warnings.append(f"{nrrd_path.name}: {axis_warning}")
                in_plane = [a for a in range(3) if a != slice_axis]
                row_axis, col_axis = in_plane[0], in_plane[1]
                pixdim_row = axis_spacing[row_axis]
                pixdim_col = axis_spacing[col_axis]
                slice_thickness = axis_spacing[slice_axis] if axis_spacing[slice_axis] > 0 else 1.0
                # Only mark calibrated when real spacing is present; otherwise stay
                # UNCALIBRATED rather than fabricate mm values.
                has_spacing = pixdim_row > 0 and pixdim_col > 0
                if not has_spacing:
                    pixdim_row = pixdim_row if pixdim_row > 0 else 1.0
                    pixdim_col = pixdim_col if pixdim_col > 0 else 1.0
                    warnings.append(f"{nrrd_path.name}: no usable spacing in header — treating as UNCALIBRATED")

                # Normalize
                d_min, d_max = float(data.min()), float(data.max())
                if d_max > d_min:
                    data_norm = ((data - d_min) / (d_max - d_min) * 255).astype(np.uint8)
                else:
                    data_norm = np.zeros_like(data, dtype=np.uint8)

                # Iterate TRUE through-plane slices (move the slice axis to the front).
                data_t = np.moveaxis(data_norm, slice_axis, 0)
                num_slices = data_t.shape[0]

                for s in range(num_slices):
                    slice_data = data_t[s]
                    dcm_filename = f"{seq_name}_Img{s+1:04d}.dcm"
                    self._create_synthetic_dicom(
                        pixel_data=slice_data,
                        output_path=self.output_dir / dcm_filename,
                        series_description=seq_name,
                        instance_number=s + 1,
                        slice_location=float(s * slice_thickness),
                        pixel_spacing=[pixdim_row, pixdim_col],
                        slice_thickness=slice_thickness,
                        rows=slice_data.shape[0],
                        cols=slice_data.shape[1],
                        study_description=f"NRRD Import: {nrrd_path.name}",
                        is_calibrated=has_spacing,
                        modality=modality,
                    )
                    total_slices += 1

                metadata[seq_name] = {
                    "source": str(nrrd_path.name),
                    "dimensions": list(data.shape),
                    "voxel_size_mm": [pixdim_row, pixdim_col, slice_thickness],
                    "slice_axis": slice_axis,
                    "num_slices": num_slices,
                }

            except Exception as e:
                warnings.append(f"Error processing {nrrd_path.name}: {str(e)}")

        return ConversionResult(
            success=total_slices > 0,
            input_format="nrrd",
            dicom_dir=str(self.output_dir),
            num_files=total_slices,
            num_slices=total_slices,
            sequences_detected=sequences,
            metadata=metadata,
            warnings=warnings,
        )

    # ── Standard Images (PNG/JPG/TIFF) ──

    def _convert_images(self) -> ConversionResult:
        """Convert image files (PNG/JPG/TIFF) to synthetic DICOM."""
        image_files = sorted([
            f for f in self.input_dir.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
        ])

        if not image_files:
            return ConversionResult(
                success=False, input_format="images", dicom_dir=str(self.output_dir),
                num_files=0, num_slices=0, error="No image files found",
            )

        warnings = []
        total_slices = 0

        # Preserve the source filename as a signal: anatomy/modality detection reads the
        # SeriesDescription + filenames, so carrying e.g. "...CXR..." through lets a plain PNG
        # X-ray still be recognized as a chest radiograph instead of an unknown "OT" blob.
        guessed_modality = "OT"
        for f in image_files:
            g = self._guess_modality_from_name(f.name)
            if g:
                guessed_modality = g
                break
        stems = sorted({re.sub(r"[^A-Za-z0-9_-]+", "_", f.stem) or "image" for f in image_files})
        study_desc = ("Image Import: " + ", ".join(stems))[:120]

        # Try to group by subdirectory or naming pattern
        # For flat directory: treat all as one sequence
        for i, img_path in enumerate(image_files):
            try:
                img = Image.open(str(img_path)).convert("L")  # Grayscale
                arr = np.array(img)

                safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", img_path.stem) or "image"
                dcm_filename = f"{safe_stem}_Img{i+1:04d}.dcm"
                self._create_synthetic_dicom(
                    pixel_data=arr,
                    output_path=self.output_dir / dcm_filename,
                    series_description=safe_stem,
                    instance_number=i + 1,
                    slice_location=float(i),
                    pixel_spacing=[1.0, 1.0],  # Unknown spacing
                    slice_thickness=1.0,
                    rows=arr.shape[0],
                    cols=arr.shape[1],
                    study_description=study_desc,
                    is_calibrated=False,
                    # Guessed from filename when possible (e.g. CXR→CR); else OT — the reader
                    # still identifies the true modality from the pixels.
                    modality=guessed_modality,
                )
                total_slices += 1

            except Exception as e:
                warnings.append(f"Error processing {img_path.name}: {str(e)}")

        # Always surface the uncalibrated warning for raw image imports (not only when
        # some other error happened) — PNG/JPG exports carry no PixelSpacing, so the
        # whole study is uncalibrated and no mm values may be reported.
        warnings.insert(0, "Image import: no DICOM calibration available — measurements will be uncalibrated (visual estimates only)")

        return ConversionResult(
            success=total_slices > 0,
            input_format="images",
            dicom_dir=str(self.output_dir),
            num_files=total_slices,
            num_slices=total_slices,
            sequences_detected=stems,
            metadata={"source_format": "images", "num_files": len(image_files)},
            warnings=warnings,
        )

    # ── ZIP Archives ──

    def _handle_zip(self) -> ConversionResult:
        """Extract ZIP and recursively detect + convert contents."""
        zip_files = [
            f for f in self.input_dir.iterdir()
            if f.is_file() and f.suffix.lower() == ".zip"
        ]

        if not zip_files:
            return ConversionResult(
                success=False, input_format="zip", dicom_dir=str(self.output_dir),
                num_files=0, num_slices=0, error="No ZIP files found",
            )

        warnings = []
        extracted_dir = self.input_dir / "_extracted"
        extracted_dir.mkdir(exist_ok=True)

        base_resolved = extracted_dir.resolve()
        # Zip-bomb guards: a few-KB archive can declare gigabytes of decompressed content, so bound both
        # the total declared uncompressed size and the member count before extracting anything.
        max_unzipped = int(os.environ.get("MIKA_MAX_UNZIPPED_BYTES", str(8 * 1024 * 1024 * 1024)))
        max_members = int(os.environ.get("MIKA_MAX_ZIP_MEMBERS", "200000"))
        for zf in zip_files:
            try:
                with zipfile.ZipFile(str(zf), "r") as z:
                    infos = z.infolist()
                    if len(infos) > max_members:
                        raise ValueError(
                            f"Archive {zf.name} has too many entries ({len(infos)}) — refusing "
                            f"(possible zip bomb)"
                        )
                    # Zip-slip guard: validate every member resolves to a path *inside* extracted_dir
                    # before writing anything. Reject absolute paths, '..' traversal, or any escaping
                    # member. Also accumulate the declared decompressed size to catch a zip bomb.
                    total_unzipped = 0
                    for info in infos:
                        member = info.filename
                        # Skip directory entries — they create no file content.
                        if member.endswith("/") or info.is_dir():
                            continue
                        norm = member.replace("\\", "/")
                        if os.path.isabs(norm) or norm.startswith("/"):
                            raise ValueError(
                                f"Refusing to extract absolute path member '{member}' "
                                f"from {zf.name} (possible zip-slip attack)"
                            )
                        target = (extracted_dir / member).resolve()
                        try:
                            common = os.path.commonpath([str(base_resolved), str(target)])
                        except ValueError:
                            # Different drives (Windows) — definitely outside the base.
                            common = ""
                        if common != str(base_resolved):
                            raise ValueError(
                                f"Refusing to extract member '{member}' from {zf.name}: "
                                f"resolved path escapes the extraction directory "
                                f"(possible zip-slip attack)"
                            )
                        total_unzipped += int(getattr(info, "file_size", 0) or 0)
                        if total_unzipped > max_unzipped:
                            raise ValueError(
                                f"Archive {zf.name} decompresses to over "
                                f"{max_unzipped // (1024 * 1024 * 1024)} GB — refusing (possible zip bomb)"
                            )
                    # All members validated and within size limits — safe to extract.
                    z.extractall(str(extracted_dir))
            except Exception as e:
                warnings.append(f"Error extracting {zf.name}: {str(e)}")

        # Flatten: move all files from subdirectories into extracted_dir
        for root, dirs, files in os.walk(str(extracted_dir)):
            root_path = Path(root)
            if root_path == extracted_dir:
                continue
            for fname in files:
                src = root_path / fname
                # Avoid name collisions
                dest_name = f"{root_path.name}_{fname}" if (extracted_dir / fname).exists() else fname
                dest = extracted_dir / dest_name
                try:
                    shutil.move(str(src), str(dest))
                except Exception:
                    pass

        # Now convert the extracted contents
        sub_converter = FormatConverter(str(extracted_dir), str(self.output_dir))
        result = sub_converter.convert()
        result.warnings = warnings + result.warnings

        # Clean up
        try:
            shutil.rmtree(str(extracted_dir))
        except Exception:
            pass

        return result

    # ── Modality Heuristic ──

    @staticmethod
    def _guess_modality_from_name(text: str) -> Optional[str]:
        """
        Conservatively guess the DICOM Modality code from a source filename and/or
        study description. Returns a DICOM modality code ('CT', 'PT', 'US', 'CR', 'MR')
        or None if no token matches.

        Matching is lowercase and word-boundary-ish so we don't misfire on substrings
        (e.g. 'ct' inside 'connect'). MR-family tokens are checked FIRST (this is an MRI-first
        app) so an explicit 'mri'/'mra'/'angio' wins, and tokens that collide with everyday words
        or MR terms — 'echo' (a normal MR term: multi-echo) and 'pt' ('patient') — are deliberately
        NOT used, since they previously misrouted real MR studies to Ultrasound/PET.
        """
        if not text:
            return None

        # Split camelCase / concatenated names so modality prefixes become their own tokens
        # ('CTChest' -> 'CT Chest', 'CTACardio' -> 'CTA Cardio', 'MRHead' -> 'MR Head'), then
        # lowercase and normalize separators (_ - . /) to spaces so \b word boundaries fire around
        # short tokens like 'ct' (underscore is a \w char, so without this 'ct' would never be bounded).
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
        spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
        lowered = re.sub(r"[_\-./\\]+", " ", spaced.lower())

        # Ordered (token_patterns, modality) — first match wins; MR first so it can't be lost to a
        # weaker token. Patterns use word-ish boundaries so 'ct' won't match inside 'connect'.
        token_map: list[tuple[list[str], str]] = [
            ([r"\bmri\b", r"\bmra\b", r"\bangio\b", r"\bmr\b"], "MR"),
            (["computed tomography", r"\bcta\b", r"\bct\b"], "CT"),
            ([r"\bpet\b"], "PT"),
            (["ultrasound", r"\bsonograph", r"\bus\b"], "US"),
            # 'cxr\b' (suffix boundary, applied as a regex) matches the chest-X-ray token even when it's
            # glued to a prefix in dataset filenames, e.g. MCUCXR_/CHNCXR_; plain '\bcxr\b' would not.
            (["radiograph", "x-ray", r"\bxray\b", r"\bxr\b", r"\bcr\b", r"\bdx\b", r"cxr\b"], "CR"),
        ]

        for patterns, modality in token_map:
            for pat in patterns:
                if pat.startswith(r"\b") or pat.endswith(r"\b"):
                    if re.search(pat, lowered):
                        return modality
                else:
                    # Multi-word phrase — match as substring on word boundary.
                    if re.search(r"\b" + re.escape(pat) + r"\b", lowered):
                        return modality

        return None

    # ── Synthetic DICOM Creator ──

    def _create_synthetic_dicom(
        self,
        pixel_data: np.ndarray,
        output_path: Path,
        series_description: str,
        instance_number: int,
        slice_location: float,
        pixel_spacing: list[float],
        slice_thickness: float,
        rows: int,
        cols: int,
        study_description: str = "",
        is_calibrated: bool = True,
        modality: str = "MR",
    ):
        """
        Create a minimal synthetic DICOM file from a 2D numpy array.
        This allows the DICOMEngine to process converted files natively.
        """
        if not PYDICOM_AVAILABLE:
            raise RuntimeError("pydicom is required for synthetic DICOM creation")

        # Create minimal DICOM dataset
        file_meta = pydicom.Dataset()
        file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"  # MR Image Storage
        file_meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

        ds = FileDataset(str(output_path), {}, file_meta=file_meta, preamble=b"\x00" * 128)

        # Patient info
        ds.PatientName = "Imported^Study"
        ds.PatientID = "MIKA_IMPORT"
        ds.PatientBirthDate = ""
        ds.PatientSex = ""

        # Study info
        ds.StudyDescription = study_description
        ds.StudyDate = ""
        ds.InstitutionName = "MIKA Import"

        # Series info
        ds.SeriesDescription = series_description
        ds.ProtocolName = series_description
        ds.Modality = modality
        ds.SeriesInstanceUID = pydicom.uid.generate_uid()
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
        ds.SOPInstanceUID = pydicom.uid.generate_uid()
        ds.StudyInstanceUID = pydicom.uid.generate_uid()

        # Geometry
        ds.InstanceNumber = instance_number
        ds.SliceLocation = slice_location
        ds.SliceThickness = slice_thickness
        if is_calibrated:
            ds.PixelSpacing = pixel_spacing

        # CT studies need a sensible default display window so downstream rendering
        # shows tissue contrast instead of a globally-normalized image. Apply a
        # soft-tissue window (C 40 / W 400) only for CT and only if not already set.
        if modality == "CT":
            if "WindowCenter" not in ds:
                ds.WindowCenter = 40
            if "WindowWidth" not in ds:
                ds.WindowWidth = 400

        # Image data
        ds.Rows = rows
        ds.Columns = cols
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        ds.PixelRepresentation = 0
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"

        # Ensure uint8
        pixel_data = pixel_data.astype(np.uint8)
        ds.PixelData = pixel_data.tobytes()

        ds.save_as(str(output_path))
