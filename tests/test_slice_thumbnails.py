"""_slice_thumbnails: resolve bare filenames + a non-DICOM fallback.

Bug: file_list holds BARE filenames (relative to the dicom dir), but the thumbnailer called
dcmread on the bare name → 'No such file' → every read failed → the Wait reading-viewer showed
nothing on a real study. Now resolved against base_dir, with a PIL fallback for PNG/JPG exports.
"""
from os.path import exists

from PIL import Image

from app import _slice_thumbnails


def test_resolves_bare_filename_against_base_dir(tmp_path):
    src = tmp_path / "dicom"
    src.mkdir()
    Image.new("L", (128, 100), 128).save(src / "img0001.png")   # a non-DICOM slice export
    out = _slice_thumbnails(["img0001.png"], str(tmp_path / "thumbs"), "k0", base_dir=str(src))
    assert len(out) == 1
    stem, path = out[0]
    assert stem.startswith("seqthumb_k0_") and exists(path)


def test_empty_or_unreadable_is_safe(tmp_path):
    assert _slice_thumbnails([], str(tmp_path), "k") == []
    # a name that resolves nowhere → skipped, never raises
    assert _slice_thumbnails(["nope.dcm"], str(tmp_path), "k", base_dir=str(tmp_path)) == []
