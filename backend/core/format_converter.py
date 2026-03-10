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

                # Extract voxel dimensions (mm)
                voxel_dims = header.get_zooms()
                pixdim_row = float(voxel_dims[0]) if len(voxel_dims) > 0 else 1.0
                pixdim_col = float(voxel_dims[1]) if len(voxel_dims) > 1 else 1.0
                slice_thickness = float(voxel_dims[2]) if len(voxel_dims) > 2 else 1.0

                # Derive sequence name from filename
                seq_name = nii_path.stem
                if seq_name.endswith(".nii"):
                    seq_name = seq_name[:-4]
                sequences.append(seq_name)

                # Handle 3D and 4D volumes
                if data.ndim == 4:
                    # 4D: use first volume
                    data = data[:, :, :, 0]
                    warnings.append(f"{nii_path.name}: 4D volume detected, using first timepoint")

                if data.ndim != 3:
                    warnings.append(f"{nii_path.name}: Expected 3D volume, got {data.ndim}D — skipping")
                    continue

                # Normalize to 0-255
                d_min, d_max = float(data.min()), float(data.max())
                if d_max > d_min:
                    data_norm = ((data - d_min) / (d_max - d_min) * 255).astype(np.uint8)
                else:
                    data_norm = np.zeros_like(data, dtype=np.uint8)

                num_slices = data_norm.shape[2]

                # Create synthetic DICOM for each slice
                for s in range(num_slices):
                    slice_data = data_norm[:, :, s]
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
                    )
                    total_slices += 1

                metadata[seq_name] = {
                    "source": str(nii_path.name),
                    "dimensions": list(data.shape),
                    "voxel_size_mm": [pixdim_row, pixdim_col, slice_thickness],
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

                # Extract spacing from NRRD header
                space_directions = header.get("space directions", None)
                if space_directions is not None:
                    try:
                        pixdim_row = float(np.linalg.norm(space_directions[0]))
                        pixdim_col = float(np.linalg.norm(space_directions[1]))
                        slice_thickness = float(np.linalg.norm(space_directions[2])) if len(space_directions) > 2 else 1.0
                    except (IndexError, TypeError):
                        pixdim_row = pixdim_col = slice_thickness = 1.0
                        warnings.append(f"{nrrd_path.name}: Could not parse space directions, using 1mm spacing")
                else:
                    spacings = header.get("spacings", [1.0, 1.0, 1.0])
                    pixdim_row = float(spacings[0]) if len(spacings) > 0 else 1.0
                    pixdim_col = float(spacings[1]) if len(spacings) > 1 else 1.0
                    slice_thickness = float(spacings[2]) if len(spacings) > 2 else 1.0

                # Handle 3D+ data
                if data.ndim == 4:
                    data = data[:, :, :, 0]
                    warnings.append(f"{nrrd_path.name}: 4D volume, using first component")

                if data.ndim != 3:
                    warnings.append(f"{nrrd_path.name}: Expected 3D, got {data.ndim}D — skipping")
                    continue

                # Normalize
                d_min, d_max = float(data.min()), float(data.max())
                if d_max > d_min:
                    data_norm = ((data - d_min) / (d_max - d_min) * 255).astype(np.uint8)
                else:
                    data_norm = np.zeros_like(data, dtype=np.uint8)

                num_slices = data_norm.shape[2]

                for s in range(num_slices):
                    slice_data = data_norm[:, :, s]
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
                    )
                    total_slices += 1

                metadata[seq_name] = {
                    "source": str(nrrd_path.name),
                    "dimensions": list(data.shape),
                    "voxel_size_mm": [pixdim_row, pixdim_col, slice_thickness],
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
        seq_name = "image_series"

        # Try to group by subdirectory or naming pattern
        # For flat directory: treat all as one sequence
        for i, img_path in enumerate(image_files):
            try:
                img = Image.open(str(img_path)).convert("L")  # Grayscale
                arr = np.array(img)

                dcm_filename = f"{seq_name}_Img{i+1:04d}.dcm"
                self._create_synthetic_dicom(
                    pixel_data=arr,
                    output_path=self.output_dir / dcm_filename,
                    series_description=seq_name,
                    instance_number=i + 1,
                    slice_location=float(i),
                    pixel_spacing=[1.0, 1.0],  # Unknown spacing
                    slice_thickness=1.0,
                    rows=arr.shape[0],
                    cols=arr.shape[1],
                    study_description=f"Image Import ({len(image_files)} files)",
                    is_calibrated=False,
                )
                total_slices += 1

            except Exception as e:
                warnings.append(f"Error processing {img_path.name}: {str(e)}")

        if warnings:
            warnings.insert(0, "Image import: no DICOM calibration available — measurements will be uncalibrated")

        return ConversionResult(
            success=total_slices > 0,
            input_format="images",
            dicom_dir=str(self.output_dir),
            num_files=total_slices,
            num_slices=total_slices,
            sequences_detected=[seq_name],
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

        for zf in zip_files:
            try:
                with zipfile.ZipFile(str(zf), "r") as z:
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
        ds.Modality = "MR"
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
