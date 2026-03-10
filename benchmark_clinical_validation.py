#!/usr/bin/env python3
"""
MIKA Clinical Validation Benchmark
====================================
End-to-end automated evaluation script that runs two clinical validation tests
against the ai-mri-analyzer (MIKA) pipeline:

  Test 1 — Sensitivity (Subtle Lesion Detection):
    Downloads one random MRI series from the TCGA-LGG (Low Grade Glioma)
    collection via the unauthenticated TCIA REST v1 API and verifies the
    pipeline correctly flags a lesion/mass/glioma (True Positive).

  Test 2 — Specificity (Hallucination / Normal Baseline):
    Downloads a healthy brain MRI from the IXI open repository, slices the
    3D NIfTI volume into 2D PNGs, and verifies the pipeline returns a clean
    normal scan without hallucinating pathology (True Negative).

Usage:
    python benchmark_clinical_validation.py

Requirements:
    - ANTHROPIC_API_KEY environment variable set
    - Python packages: requests, nibabel, matplotlib, numpy
      (plus all MIKA backend dependencies)
"""

import io
import os
import sys
import json
import time
import shutil
import random
import logging
import zipfile
import tarfile
import tempfile
import traceback
from pathlib import Path
from typing import Optional

import requests
import numpy as np

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mika.benchmark")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TCIA_BASE = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
TCIA_COLLECTION = "TCGA-LGG"
IXI_T1_URL = "http://biomedic.doc.ic.ac.uk/brain-development/downloads/IXI/IXI-T1.tar"

BENCHMARK_DIR = Path(__file__).resolve().parent / "benchmark_data"
SUBTLE_LESION_DIR = BENCHMARK_DIR / "test_subtle_lesion"
NORMAL_BRAIN_DIR = BENCHMARK_DIR / "test_normal_brain"

# How many 2D slices to extract from the IXI NIfTI volume
IXI_MAX_SLICES = 60

# Network retry parameters
MAX_RETRIES = 4
BACKOFF_BASE = 2  # seconds

# Pathology keywords for sensitivity check (Test 1 — True Positive)
PATHOLOGY_KEYWORDS = [
    "lesion", "mass", "glioma", "tumor", "tumour", "neoplasm",
    "enhancement", "abnormal", "signal abnormality", "hyperintense",
    "t2 hyperintens", "flair hyperintens", "edema", "oedema",
    "infiltrat", "expansile", "space-occupying", "patholog",
    "grade", "who grade", "astrocytoma", "oligodendroglioma",
]

# Hallucination keywords for specificity check (Test 2 — True Negative)
HALLUCINATION_KEYWORDS = [
    "tumor", "tumour", "mass", "lesion", "glioma", "neoplasm",
    "infarct", "ischemi", "stroke", "hemorrhage", "haemorrhage",
    "bleed", "edema", "oedema", "abscess", "metastas",
    "demyelinat", "multiple sclerosis", "enhancing",
]


# ---------------------------------------------------------------------------
# Network helpers with retry + exponential backoff
# ---------------------------------------------------------------------------
def _request_with_retry(
    method: str,
    url: str,
    *,
    max_retries: int = MAX_RETRIES,
    timeout: int = 120,
    stream: bool = False,
    **kwargs,
) -> requests.Response:
    """HTTP request with exponential-backoff retry on network errors."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(
                method, url, timeout=timeout, stream=stream, **kwargs
            )
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = BACKOFF_BASE ** attempt
                log.warning(
                    "Request to %s failed (attempt %d/%d): %s — retrying in %ds",
                    url, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)
            else:
                log.error(
                    "Request to %s failed after %d attempts: %s",
                    url, max_retries, exc,
                )
    raise RuntimeError(f"All {max_retries} attempts failed for {url}") from last_exc


# ---------------------------------------------------------------------------
# Test 1 — Download TCGA-LGG series from TCIA
# ---------------------------------------------------------------------------
def download_tcia_lgg_series(output_dir: Path) -> bool:
    """
    Fetch one random MRI series from the TCGA-LGG collection using the
    unauthenticated TCIA v1 REST API, then download & extract DICOMs.

    API docs: https://wiki.cancerimagingarchive.net/display/Public/TCIA+Current+REST+API+Guide
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Get patient list
    log.info("TCIA: Fetching patient list for collection '%s'...", TCIA_COLLECTION)
    resp = _request_with_retry(
        "GET",
        f"{TCIA_BASE}/getPatient",
        params={"Collection": TCIA_COLLECTION, "format": "json"},
    )
    patients = resp.json()
    if not patients:
        log.error("No patients found in TCIA collection %s", TCIA_COLLECTION)
        return False
    log.info("TCIA: Found %d patients in %s", len(patients), TCIA_COLLECTION)

    # Pick a random patient
    patient = random.choice(patients)
    patient_id = patient["PatientID"]
    log.info("TCIA: Selected patient %s", patient_id)

    # Step 2: Get studies for that patient
    resp = _request_with_retry(
        "GET",
        f"{TCIA_BASE}/getPatientStudy",
        params={
            "Collection": TCIA_COLLECTION,
            "PatientID": patient_id,
            "format": "json",
        },
    )
    studies = resp.json()
    if not studies:
        log.error("No studies found for patient %s", patient_id)
        return False

    study_uid = studies[0]["StudyInstanceUID"]
    log.info("TCIA: Using study %s", study_uid)

    # Step 3: Get series list, prefer MR modality
    resp = _request_with_retry(
        "GET",
        f"{TCIA_BASE}/getSeries",
        params={
            "Collection": TCIA_COLLECTION,
            "PatientID": patient_id,
            "StudyInstanceUID": study_uid,
            "Modality": "MR",
            "format": "json",
        },
    )
    series_list = resp.json()
    if not series_list:
        log.error("No MR series found for study %s", study_uid)
        return False
    log.info("TCIA: Found %d MR series", len(series_list))

    # Pick one series (prefer T2/FLAIR if available, else random)
    chosen = None
    for s in series_list:
        desc = (s.get("SeriesDescription") or "").lower()
        if any(k in desc for k in ["t2", "flair", "t2w"]):
            chosen = s
            break
    if not chosen:
        chosen = random.choice(series_list)

    series_uid = chosen["SeriesInstanceUID"]
    log.info(
        "TCIA: Downloading series '%s' (%s, %s images)...",
        chosen.get("SeriesDescription", "N/A"),
        series_uid,
        chosen.get("ImageCount", "?"),
    )

    # Step 4: Download the DICOM images for that series
    resp = _request_with_retry(
        "GET",
        f"{TCIA_BASE}/getImage",
        params={"SeriesInstanceUID": series_uid},
        stream=True,
        timeout=300,
    )

    # The response is a ZIP file of DICOMs
    zip_bytes = io.BytesIO(resp.content)
    try:
        with zipfile.ZipFile(zip_bytes) as zf:
            zf.extractall(str(output_dir))
    except zipfile.BadZipFile:
        # Some endpoints return raw DICOM concatenated — save as single file
        (output_dir / "series.dcm").write_bytes(resp.content)

    dicom_count = sum(1 for f in output_dir.rglob("*") if f.is_file())
    log.info(
        "TCIA: Extracted %d files to %s",
        dicom_count,
        output_dir,
    )
    return dicom_count > 0


# ---------------------------------------------------------------------------
# Test 2 — Download IXI healthy brain NIfTI and slice to 2D PNGs
# ---------------------------------------------------------------------------
def download_ixi_normal_brain(output_dir: Path) -> bool:
    """
    Download the IXI-T1 dataset (tar of NIfTI files), extract one patient's
    volume, and slice the 3D volume into 2D axial PNG images.
    """
    import nibabel as nib
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=True)
    tar_path = BENCHMARK_DIR / "IXI-T1.tar"

    # Step 1: Download the tar (large file — stream to disk)
    if not tar_path.exists():
        log.info("IXI: Downloading IXI-T1.tar (this may take several minutes)...")
        try:
            resp = _request_with_retry("GET", IXI_T1_URL, stream=True, timeout=600)
            with open(str(tar_path), "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            log.info("IXI: Downloaded %.1f MB", tar_path.stat().st_size / 1e6)
        except Exception as exc:
            log.error("IXI: Failed to download IXI-T1.tar: %s", exc)
            # Clean up partial download
            if tar_path.exists():
                tar_path.unlink()
            return False
    else:
        log.info("IXI: Using cached IXI-T1.tar (%.1f MB)", tar_path.stat().st_size / 1e6)

    # Step 2: Extract a single NIfTI file from the tar
    log.info("IXI: Extracting one NIfTI volume from tar...")
    nifti_path = None
    try:
        with tarfile.open(str(tar_path), "r") as tf:
            for member in tf:
                if member.name.endswith(".nii.gz") or member.name.endswith(".nii"):
                    tf.extract(member, str(BENCHMARK_DIR))
                    nifti_path = BENCHMARK_DIR / member.name
                    log.info("IXI: Extracted %s", member.name)
                    break
    except tarfile.TarError as exc:
        log.error("IXI: Failed to extract tar: %s", exc)
        return False

    if not nifti_path or not nifti_path.exists():
        log.error("IXI: No NIfTI file found in tar archive")
        return False

    # Step 3: Load the 3D volume and slice into 2D axial PNGs
    log.info("IXI: Loading NIfTI volume and slicing into 2D images...")
    img = nib.load(str(nifti_path))
    data = np.asarray(img.dataobj, dtype=np.float32)
    log.info("IXI: Volume shape = %s", data.shape)

    # Take axial slices (along the 3rd axis, which is typically axial for T1)
    n_slices = data.shape[2]
    # Skip the top/bottom ~20% to avoid mostly-empty slices
    start = int(n_slices * 0.2)
    end = int(n_slices * 0.8)
    slice_indices = np.linspace(start, end - 1, min(IXI_MAX_SLICES, end - start), dtype=int)

    saved = 0
    for idx in slice_indices:
        slice_2d = data[:, :, idx]
        # Normalize to 0-255
        s_min, s_max = slice_2d.min(), slice_2d.max()
        if s_max - s_min < 1e-6:
            continue
        slice_norm = ((slice_2d - s_min) / (s_max - s_min) * 255).astype(np.uint8)

        fig, ax = plt.subplots(1, 1, figsize=(5, 5), dpi=100)
        ax.imshow(slice_norm.T, cmap="gray", origin="lower")
        ax.axis("off")
        out_path = output_dir / f"axial_slice_{idx:04d}.png"
        fig.savefig(str(out_path), bbox_inches="tight", pad_inches=0, dpi=100)
        plt.close(fig)
        saved += 1

    log.info("IXI: Saved %d axial slices to %s", saved, output_dir)

    # Clean up extracted NIfTI to save disk space (keep the tar for reruns)
    try:
        nifti_path.unlink()
    except OSError:
        pass

    return saved > 0


# ---------------------------------------------------------------------------
# Pipeline execution via MIKA FastAPI TestClient
# ---------------------------------------------------------------------------
def run_pipeline(input_dir: Path, test_name: str) -> Optional[dict]:
    """
    Run the MIKA pipeline on a directory of images and return the report dict.
    Uses FastAPI's TestClient to invoke the API endpoints in-process.
    """
    from starlette.testclient import TestClient

    # Ensure we can import the backend
    backend_dir = Path(__file__).resolve().parent / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from app import app  # noqa: E402

    client = TestClient(app)
    files_in_dir = sorted(input_dir.rglob("*"))
    files_in_dir = [f for f in files_in_dir if f.is_file()]

    if not files_in_dir:
        log.error("[%s] No files found in %s", test_name, input_dir)
        return None

    log.info("[%s] Uploading %d files to MIKA pipeline...", test_name, len(files_in_dir))

    # Upload files (multipart form)
    upload_files = []
    for f in files_in_dir:
        upload_files.append(("files", (f.name, open(str(f), "rb"))))

    try:
        resp = client.post("/api/upload", files=upload_files)
    finally:
        # Close file handles
        for _, (_, fh) in upload_files:
            fh.close()

    if resp.status_code != 200:
        log.error("[%s] Upload failed (%d): %s", test_name, resp.status_code, resp.text)
        return None

    upload_data = resp.json()
    job_id = upload_data["job_id"]
    log.info(
        "[%s] Upload OK — job_id=%s, format=%s, files=%d",
        test_name, job_id, upload_data.get("input_format"), upload_data.get("file_count"),
    )

    # Start analysis
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("[%s] ANTHROPIC_API_KEY not set — cannot run Claude interpretation", test_name)
        return None

    resp = client.post(
        "/api/analyze",
        json={
            "job_id": job_id,
            "api_key": api_key,
            "clinical_history": f"Benchmark test: {test_name}",
        },
    )
    if resp.status_code != 200:
        log.error("[%s] Analyze failed (%d): %s", test_name, resp.status_code, resp.text)
        return None

    log.info("[%s] Analysis started, polling for completion...", test_name)

    # Poll until complete (TestClient runs background tasks synchronously in
    # some configurations, but we poll to be safe)
    for _ in range(120):  # up to 2 minutes
        status_resp = client.get(f"/api/status/{job_id}")
        if status_resp.status_code != 200:
            break
        status = status_resp.json()
        if status["status"] == "complete":
            log.info("[%s] Analysis complete!", test_name)
            break
        if status["status"] == "error":
            log.error("[%s] Analysis error: %s", test_name, status.get("error"))
            return None
        time.sleep(1)
    else:
        log.error("[%s] Analysis timed out", test_name)
        return None

    # Retrieve report
    resp = client.get(f"/api/report/{job_id}")
    if resp.status_code != 200:
        log.error("[%s] Report retrieval failed (%d): %s", test_name, resp.status_code, resp.text)
        return None

    report = resp.json()
    log.info("[%s] Report retrieved — anatomy=%s", test_name, report.get("detected_anatomy"))
    return report


# ---------------------------------------------------------------------------
# Result evaluation
# ---------------------------------------------------------------------------
def _flatten_report_text(report: dict) -> str:
    """Recursively extract all string values from the report and join them."""
    parts: list[str] = []

    def _walk(obj):
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                _walk(v)

    _walk(report.get("interpretation", {}))
    return "\n".join(parts).lower()


def evaluate_sensitivity(report: dict) -> tuple[bool, str]:
    """
    Test 1 — True Positive: Did the pipeline detect pathology in the
    TCGA-LGG (glioma) dataset?

    Pass = at least one pathology keyword found in the interpretation.
    """
    text = _flatten_report_text(report)
    matched = [kw for kw in PATHOLOGY_KEYWORDS if kw in text]
    if matched:
        return True, f"Pathology detected (matched: {', '.join(matched[:5])})"
    return False, "No pathology keywords found in interpretation"


def evaluate_specificity(report: dict) -> tuple[bool, str]:
    """
    Test 2 — True Negative: Did the pipeline avoid hallucinating pathology
    on a healthy IXI brain?

    Pass = NONE of the hallucination keywords appear in the interpretation.
    """
    text = _flatten_report_text(report)
    matched = [kw for kw in HALLUCINATION_KEYWORDS if kw in text]
    if matched:
        return False, f"Hallucinated pathology (matched: {', '.join(matched[:5])})"
    return True, "Clean report — no hallucinated pathology"


# ---------------------------------------------------------------------------
# Pretty-print evaluation matrix
# ---------------------------------------------------------------------------
def print_evaluation_matrix(results: list[dict]) -> None:
    """Print a formatted evaluation matrix."""
    border = "=" * 78
    divider = "-" * 78

    print(f"\n{border}")
    print("  MIKA CLINICAL VALIDATION — EVALUATION MATRIX")
    print(border)
    print(f"  {'Test':<36} {'Metric':<14} {'Result':<8} {'Details'}")
    print(divider)

    all_pass = True
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        marker = "\u2713" if r["passed"] else "\u2717"
        print(f"  {r['name']:<36} {r['metric']:<14} {marker} {status:<5}  {r['detail']}")
        if not r["passed"]:
            all_pass = False

    print(divider)
    overall = "ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED"
    print(f"  Overall: {overall}")
    print(f"{border}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    log.info("Starting MIKA Clinical Validation Benchmark")
    log.info("Benchmark data directory: %s", BENCHMARK_DIR)

    # Pre-flight: check for API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "The MIKA pipeline requires it for Claude interpretation."
        )
        return 1

    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    # ------------------------------------------------------------------
    # Test 1: Sensitivity — TCGA-LGG Glioma (Subtle Lesion Detection)
    # ------------------------------------------------------------------
    log.info("=" * 60)
    log.info("TEST 1: Sensitivity — Subtle Lesion Detection (TCGA-LGG)")
    log.info("=" * 60)

    test1_passed = False
    test1_detail = "Not executed"

    try:
        # Download
        if not any(SUBTLE_LESION_DIR.rglob("*")) if SUBTLE_LESION_DIR.exists() else True:
            ok = download_tcia_lgg_series(SUBTLE_LESION_DIR)
            if not ok:
                test1_detail = "Failed to download TCIA TCGA-LGG data"
                raise RuntimeError(test1_detail)
        else:
            log.info("Using cached TCGA-LGG data in %s", SUBTLE_LESION_DIR)

        # Run pipeline
        report = run_pipeline(SUBTLE_LESION_DIR, "Test1-Sensitivity")
        if report is None:
            test1_detail = "Pipeline execution failed"
            raise RuntimeError(test1_detail)

        # Evaluate
        test1_passed, test1_detail = evaluate_sensitivity(report)
        log.info("Test 1 result: %s — %s", "PASS" if test1_passed else "FAIL", test1_detail)

    except Exception as exc:
        log.error("Test 1 encountered an error: %s", exc)
        test1_detail = f"Error: {exc}"
        traceback.print_exc()

    results.append({
        "name": "Sensitivity (Subtle Lesion / LGG)",
        "metric": "True Positive",
        "passed": test1_passed,
        "detail": test1_detail,
    })

    # ------------------------------------------------------------------
    # Test 2: Specificity — IXI Normal Brain (Hallucination Baseline)
    # ------------------------------------------------------------------
    log.info("=" * 60)
    log.info("TEST 2: Specificity — Normal Brain Baseline (IXI)")
    log.info("=" * 60)

    test2_passed = False
    test2_detail = "Not executed"

    try:
        # Download & slice
        if not any(NORMAL_BRAIN_DIR.rglob("*.png")) if NORMAL_BRAIN_DIR.exists() else True:
            ok = download_ixi_normal_brain(NORMAL_BRAIN_DIR)
            if not ok:
                test2_detail = "Failed to download/process IXI data"
                raise RuntimeError(test2_detail)
        else:
            log.info("Using cached IXI slices in %s", NORMAL_BRAIN_DIR)

        # Run pipeline
        report = run_pipeline(NORMAL_BRAIN_DIR, "Test2-Specificity")
        if report is None:
            test2_detail = "Pipeline execution failed"
            raise RuntimeError(test2_detail)

        # Evaluate
        test2_passed, test2_detail = evaluate_specificity(report)
        log.info("Test 2 result: %s — %s", "PASS" if test2_passed else "FAIL", test2_detail)

    except Exception as exc:
        log.error("Test 2 encountered an error: %s", exc)
        test2_detail = f"Error: {exc}"
        traceback.print_exc()

    results.append({
        "name": "Specificity (Normal Brain / IXI)",
        "metric": "True Negative",
        "passed": test2_passed,
        "detail": test2_detail,
    })

    # ------------------------------------------------------------------
    # Final evaluation matrix
    # ------------------------------------------------------------------
    print_evaluation_matrix(results)

    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
