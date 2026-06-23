"""Phase 1 — converter slice-axis selection.

The .mha/.nii converters used to iterate array axis 2 (or sitk axis 0) blindly, which
transposed a ~50-slice sagittal study into 578 thin "slices" and mislabelled every disc
level. choose_slice_axis picks the through-plane axis from voxel spacing instead.
"""
import pytest

from core.format_converter import choose_slice_axis


def test_anisotropic_picks_largest_spacing_axis():
    # Sagittal lumbar study aligned (z, y, x) = (SI, AP, LR); LR is the slice axis.
    axis, warning = choose_slice_axis((578, 448, 50), (0.6, 0.6, 3.3))
    assert axis == 2                 # largest spacing = through-plane = the 50-slice axis
    assert warning is None           # confident, anisotropic


def test_slice_axis_can_be_axis_zero():
    axis, warning = choose_slice_axis((24, 256, 256), (5.0, 0.9, 0.9))
    assert axis == 0
    assert warning is None


def test_near_isotropic_falls_back_to_fewest_slices_and_warns():
    # Truly isotropic voxels — spacing can't name the slice axis, so use the smallest dim.
    axis, warning = choose_slice_axis((256, 256, 24), (1.0, 1.0, 1.0))
    assert axis == 2                 # argmin(shape) = the 24-sample axis
    assert warning and "isotropic" in warning.lower()


def test_almost_isotropic_within_ratio_still_warns():
    axis, warning = choose_slice_axis((300, 30, 300), (0.9, 1.0, 0.9))
    assert axis == 1                 # fewest samples
    assert warning is not None


def test_578_signature_guard_fires_loudly():
    # A chosen through-plane axis with hundreds of sub-2mm slices is the transpose symptom.
    axis, warning = choose_slice_axis((500, 100, 100), (1.9, 0.6, 0.6))
    assert axis == 0                 # argmax(spacing)
    assert warning and "transposed" in warning.lower()


def test_uncalibrated_volume_still_uses_fewest_slices():
    # No spacing (all zero / fallback) → isotropic branch → fewest-sample axis as slices.
    axis, warning = choose_slice_axis((578, 448, 50), (0.0, 0.0, 0.0))
    assert axis == 2
    assert warning is not None


def test_bad_dimensionality_raises():
    with pytest.raises(ValueError):
        choose_slice_axis((10, 10), (1.0, 1.0))
    with pytest.raises(ValueError):
        choose_slice_axis((10, 10, 10, 10), (1.0, 1.0, 1.0, 1.0))
