"""Demo: Phase-5 annotation rendering at correct (ground-truth) disc positions.

Writes a PERSISTENT image to cache_spine/ (gitignored) so it can be viewed any time:
    cd backend && PYTHONPATH=. python -m validation.demo_annotations
Output: backend/validation/cache_spine/demo_annotated.png
"""
import sys, tempfile
from pathlib import Path
import numpy as np
import SimpleITK as sitk
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from core.annotation_renderer import render_all  # noqa: E402
from core.dicom_engine import DICOMEngine  # noqa: E402
from validation import spine_eval  # noqa: E402

SPIDER = Path("C:/mika_data/spider")
OUT_DIR = HERE / "cache_spine"


def main():
    case = spine_eval.discover_spider_cases(SPIDER, limit=1)[0]
    work = Path(tempfile.mkdtemp())
    dd, conv = spine_eval.prepare_case(case, work)
    eng = DICOMEngine(str(dd), str(work / "inv")); eng.run_inventory(); eng.convert_sequences(["1_t2"])
    pngs = sorted((work / "inv" / "raw_png" / "1_t2").glob("slice_*.png"),
                  key=lambda p: int(p.stem.split("_")[1]))
    stack = np.stack([np.array(Image.open(p).convert("L")).astype(float) for p in pngs])
    n, H, W = stack.shape
    lower = stack[:, int(H * 0.62):, int(W * 0.30):int(W * 0.70)].mean(axis=(1, 2))
    best = int(lower.argmax())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = OUT_DIR / "demo_base.png"
    Image.fromarray(stack[best].astype("uint8")).save(base)

    msk = sitk.GetArrayFromImage(sitk.ReadImage(str(SPIDER / "masks" / "1_t2.mha")))
    labmap = {207: "L5-S1", 206: "L4-L5", 205: "L3-L4", 203: "L1-L2"}
    pos = {}
    for lab, name in labmap.items():
        idx = np.argwhere(msk == lab)
        if len(idx):
            pos[name] = (float(idx[:, 1].mean()), float(idx[:, 0].mean()))

    specs = [
        {"form": "circle", "center": list(pos["L5-S1"]), "radius": 16,
         "label": "L5-S1 herniation", "number": 6.2, "units": "mm",
         "certainty": "Confirmed", "significance": 0.95, "calibrated": True},
        {"form": "arrow", "point": list(pos["L4-L5"]),
         "label": "L4-L5 bulge", "certainty": "Likely", "significance": 0.8},
        {"form": "box", "bbox": [pos["L3-L4"][0] - 26, pos["L3-L4"][1] - 15,
                                 pos["L3-L4"][0] + 26, pos["L3-L4"][1] + 15],
         "label": "L3-L4 fissure (approx)", "certainty": "Possible", "significance": 0.5},
        {"form": "leader", "point": list(pos["L1-L2"]),
         "label": "L1-L2 normal", "certainty": "Reference", "significance": 0.3},
    ]
    out = OUT_DIR / "demo_annotated.png"
    render_all(base, specs, out, scale=3, calibrated=True,
               title="Sagittal T2 — model-chosen annotations", legend=True)
    print("saved (gitignored, persistent):", out)


if __name__ == "__main__":
    main()
