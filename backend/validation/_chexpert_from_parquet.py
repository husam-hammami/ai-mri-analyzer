"""Materialize the CheXpert validation split (HF mirror parquet) into the on-disk layout
chexpert_eval.py expects: test_data/chexpert/valid.csv + test_data/chexpert/valid/<...>.jpg.
Label class index -> CheXpert convention: 0 unlabeled->'' , 1 uncertain->-1 , 2 absent->0 , 3 present->1.
"""
from pathlib import Path
import csv
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

LABELS = ["No Finding", "Enlarged Cardiomediastinum", "Cardiomegaly", "Lung Opacity", "Lung Lesion",
          "Edema", "Consolidation", "Pneumonia", "Atelectasis", "Pneumothorax", "Pleural Effusion",
          "Pleural Other", "Fracture", "Support Devices"]
MAP = {0: "", 1: "-1", 2: "0", 3: "1"}

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "test_data" / "chexpert"
DATA.mkdir(parents=True, exist_ok=True)

p = hf_hub_download(repo_id="danjacobellis/chexpert", repo_type="dataset",
                    filename="default/validation/0000.parquet", revision="refs/convert/parquet")
t = pq.read_table(p).to_pylist()
print("rows:", len(t))

rows_out = []
for r in t:
    path = r["Path"]                      # CheXpert-v1.0-small/valid/patient.../viewN_*.jpg
    rel = "/".join(path.split("/")[1:])   # valid/patient.../viewN_*.jpg
    dst = DATA / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(r["image"]["bytes"])
    out = {"Path": path}
    for lab in LABELS:
        out[lab] = MAP.get(int(r[lab]), "")
    rows_out.append(out)

with open(DATA / "valid.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["Path"] + LABELS)
    w.writeheader()
    w.writerows(rows_out)

studies = {"/".join(r["Path"].split("/")[-3:-1]) for r in t}
print(f"wrote {DATA/'valid.csv'} | {len(rows_out)} images | {len(studies)} studies")
