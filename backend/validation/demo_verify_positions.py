"""Verify L5-S1 identity: on the slice that best shows it, outline the disc (207) plus the
vertebra above (L5=6) and below (sacrum/S1=7) so the disc IDENTITY is provable, not assumed.
Output to cache_spine/ (gitignored, persistent)."""
import sys, tempfile
from pathlib import Path
import numpy as np
import SimpleITK as sitk
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from core.dicom_engine import DICOMEngine  # noqa: E402
from validation import spine_eval  # noqa: E402

SPIDER = Path("C:/mika_data/spider")
OUT = HERE / "cache_spine"


def main():
    case = spine_eval.discover_spider_cases(SPIDER, limit=1)[0]
    work = Path(tempfile.mkdtemp())
    dd, conv = spine_eval.prepare_case(case, work)
    eng = DICOMEngine(str(dd), str(work / "inv")); eng.run_inventory(); eng.convert_sequences(["1_t2"])
    pngs = sorted((work / "inv" / "raw_png" / "1_t2").glob("slice_*.png"),
                  key=lambda p: int(p.stem.split("_")[1]))
    stack = np.stack([np.array(Image.open(p).convert("L")) for p in pngs])

    msk = sitk.GetArrayFromImage(sitk.ReadImage(str(SPIDER / "masks" / "1_t2.mha")))  # (SI,AP,LR)
    # slice where the L5-S1 disc (label 207) is MOST in-plane
    per_slice = [(int((msk[:, :, x] == 207).sum()), x) for x in range(msk.shape[2])]
    best = max(per_slice)[1]
    print("slice best showing L5-S1 (label 207):", best, "with", max(per_slice)[0], "disc px")

    img = Image.fromarray(stack[best]).convert("RGB")
    d = ImageDraw.Draw(img)
    # outline each structure on this slice by its bounding box + label
    parts = {6: ("L5 body", (90, 200, 255)), 207: ("L5-S1 DISC", (40, 230, 40)),
             7: ("S1 / sacrum", (255, 140, 40))}
    for lab, (name, col) in parts.items():
        on = np.argwhere(msk[:, :, best] == lab)
        if not len(on):
            print(f"  {name}: not on this slice"); continue
        y0, x0 = on[:, 0].min(), on[:, 1].min()
        y1, x1 = on[:, 0].max(), on[:, 1].max()
        d.rectangle([int(x0), int(y0), int(x1), int(y1)], outline=col, width=2)
        d.text((int(x1) + 4, int((y0 + y1) / 2) - 6), name, fill=col)
        print(f"  {name}: SI rows [{y0},{y1}]  (label {lab})")

    out = OUT / "l5s1_identity_proof.png"
    OUT.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print("\nGREEN box = the L5-S1 disc; BLUE = L5 above it; ORANGE = sacrum below it ->", out)


if __name__ == "__main__":
    main()
