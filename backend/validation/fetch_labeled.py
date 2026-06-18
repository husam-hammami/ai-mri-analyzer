"""
Fetch LABELED public imaging studies (known diagnoses) for MIKA reading-accuracy testing.

Every study here has a real ground-truth finding:
  - 5 TCIA collection-level cancers (the collection IS the label; every patient confirmed):
      UPENN-GBM glioblastoma (brain MR), TCGA-LUAD lung adenocarcinoma (chest CT),
      TCGA-LIHC hepatocellular carcinoma (liver MR), TCGA-KIRC renal clear-cell ca (kidney MR),
      QIN-PROSTATE prostate carcinoma (prostate MR/ADC)
  - NLM TB chest X-ray pair: Montgomery TB-positive + Shenzhen normal (label in filename + clinical .txt)
  - MSD Task04 normal hippocampus (MR) — normal-structure control

All sources are public, no-login, no-clickwrap (CC-BY / public-domain). Live-verified via a research
workflow (TCIA DICM magic bytes + byte counts confirmed). Data lands under test_data/labeled/<name>/study/
(only ingestible images) with labels in <name>/_truth/. Writes ground_truth_labeled.json for the harness.

Run from backend/ :  python -m validation.fetch_labeled
Then:                python -m validation.validate            # free detection
                     python -m validation.validate --read     # reading accuracy (costs credits)
"""
import io
import json
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
LABELED = REPO / "test_data" / "labeled"
TCIA = "https://services.cancerimagingarchive.net/nbia-api/services/v1/getImage?SeriesInstanceUID="
NLM = "https://data.lhncbc.nlm.nih.gov/public/Tuberculosis-Chest-X-ray-Datasets"
UA = {"User-Agent": "Mozilla/5.0 (MIKA-validation fetcher)"}

# type: "tcia_zip" (DICOM zip -> study/), "image" (single file -> study/),
#       "msd_image" (one image member of a tar -> study/, optional label member -> _truth/),
#       "text" (label/metadata -> _truth/)
MANIFEST = [
    {"name": "tcia-upenn-gbm-brain-flair", "anatomy": "brain", "modality": "MR",
     "diagnosis": "Glioblastoma (GBM, WHO grade IV) — UPENN-GBM; axial FLAIR",
     "type": "tcia_zip", "uid": "1.3.6.1.4.1.14519.5.2.1.14309413670656709985357457896955716298",
     "expect_findings": [{"label": "brain tumor / glioma",
                          "keywords": ["glioblastoma", "gbm", "glioma", "tumor", "mass", "neoplasm",
                                       "lesion", "edema", "necrosis", "mass effect"]}],
     "expect_absent": ["normal brain", "no abnormality", "unremarkable", "no mass"]},

    {"name": "tcia-tcga-luad-chest-ct", "anatomy": "chest", "modality": "CT",
     "diagnosis": "Lung adenocarcinoma (NSCLC) — TCGA-LUAD; chest CT",
     "type": "tcia_zip", "uid": "1.3.6.1.4.1.14519.5.2.1.8421.9002.433116391104490701587500870084",
     "expect_findings": [{"label": "lung cancer / nodule / mass",
                          "keywords": ["adenocarcinoma", "lung cancer", "nsclc", "nodule", "mass",
                                       "tumor", "neoplasm", "lesion", "spiculated"]}],
     "expect_absent": ["clear lungs", "no nodule", "no mass", "normal chest"]},

    {"name": "tcia-tcga-lihc-liver-mr", "anatomy": "abdomen", "modality": "MR",
     "diagnosis": "Hepatocellular carcinoma (HCC) — TCGA-LIHC; axial T2",
     "type": "tcia_zip", "uid": "1.3.6.1.4.1.14519.5.2.1.8421.4008.269080758102339313623726475389",
     "expect_findings": [{"label": "liver mass / HCC",
                          "keywords": ["hepatocellular", "hcc", "liver cancer", "hepatic mass",
                                       "liver lesion", "mass", "tumor", "lesion", "cirrhosis"]}],
     "expect_absent": ["normal liver", "no hepatic lesion", "no mass", "unremarkable abdomen"]},

    {"name": "tcia-tcga-kirc-kidney-mr", "anatomy": "abdomen", "modality": "MR",
     "diagnosis": "Renal clear-cell carcinoma (ccRCC) — TCGA-KIRC; coronal T2",
     "type": "tcia_zip", "uid": "1.3.6.1.4.1.14519.5.2.1.9203.4004.841019447160935770385435055363",
     "expect_findings": [{"label": "renal mass / RCC",
                          "keywords": ["renal cell carcinoma", "clear cell", "ccrcc", "kidney cancer",
                                       "renal mass", "renal tumor", "mass", "lesion", "tumor"]}],
     "expect_absent": ["normal kidneys", "no renal mass", "no lesion", "unremarkable"]},

    {"name": "tcia-qin-prostate-mr", "anatomy": "prostate", "modality": "MR",
     "diagnosis": "Prostate carcinoma — QIN-PROSTATE; ADC map",
     "type": "tcia_zip", "uid": "1.3.6.1.4.1.14519.5.2.1.3671.4754.141342195149605473745656780893",
     "expect_findings": [{"label": "prostate cancer",
                          "keywords": ["prostate cancer", "carcinoma", "adenocarcinoma", "peripheral zone",
                                       "restricted diffusion", "low adc", "pi-rads", "lesion", "tumor"]}],
     "expect_absent": ["normal prostate", "no lesion", "benign", "unremarkable"]},

    {"name": "nlm-montgomery-cxr-tb", "anatomy": "chest", "modality": "CR",
     "diagnosis": "Pulmonary tuberculosis (Montgomery MCUCXR_0104_1, label _1 = TB-positive)",
     "type": "image", "url": f"{NLM}/Montgomery-County-CXR-Set/MontgomerySet/CXR_png/MCUCXR_0104_1.png",
     "out": "MCUCXR_0104_1.png",
     "label_url": f"{NLM}/Montgomery-County-CXR-Set/MontgomerySet/ClinicalReadings/MCUCXR_0104_1.txt",
     "label_out": "clinical_reading.txt",
     "expect_findings": [{"label": "tuberculosis / infiltrate",
                          "keywords": ["tuberculosis", "tb", "infiltrate", "consolidation", "opacity",
                                       "scarring", "fibrosis", "apical", "upper lobe", "cavitation"]}],
     "expect_absent": []},

    {"name": "nlm-shenzhen-cxr-normal", "anatomy": "chest", "modality": "CR",
     "diagnosis": "Normal chest radiograph (Shenzhen CHNCXR_0001_0, label _0 = normal) — overcall control",
     "type": "image", "url": f"{NLM}/Shenzhen-Hospital-CXR-Set/CXR_png/CHNCXR_0001_0.png",
     "out": "CHNCXR_0001_0.png",
     "expect_findings": [{"label": "normal / clear",
                          "keywords": ["normal", "clear", "no acute", "unremarkable", "no consolidation"]}],
     "expect_absent": ["tuberculosis", "consolidation", "cavitation", "mass", "tumor", "effusion"]},

    {"name": "msd-hippocampus-mr", "anatomy": "brain", "modality": "MR",
     "diagnosis": "Normal hippocampus MR (MSD Task04, no pathology) — overcall control",
     "type": "msd_image", "tar_url": "https://msd-for-monai.s3-us-west-2.amazonaws.com/Task04_Hippocampus.tar",
     "image_member": "Task04_Hippocampus/imagesTr/hippocampus_001.nii.gz", "out": "hippocampus_001.nii.gz",
     "label_member": "Task04_Hippocampus/labelsTr/hippocampus_001.nii.gz", "label_out": "label.nii.gz",
     "expect_findings": [{"label": "brain / medial temporal",
                          "keywords": ["hippocamp", "temporal", "brain", "normal"]}],
     "expect_absent": ["tumor", "mass", "glioma", "hemorrhage", "infarct", "metastasis"]},
]


def _get(url: str, timeout: int = 180) -> bytes:
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"    retry {attempt + 1}/3 ({e})")
            time.sleep(2 * (attempt + 1))
    raise last


def fetch(entry: dict) -> dict:
    name = entry["name"]
    base = LABELED / name
    study = base / "study"
    truth = base / "_truth"
    study.mkdir(parents=True, exist_ok=True)
    print(f"- {name} ({entry['type']})")

    if entry["type"] == "tcia_zip":
        data = _get(TCIA + entry["uid"])
        zf = zipfile.ZipFile(io.BytesIO(data))
        n = 0
        for m in zf.namelist():
            if m.endswith("/"):
                continue
            target = study / Path(m).name
            target.write_bytes(zf.read(m))
            if Path(m).suffix.lower() in (".dcm", "") or "." not in Path(m).name:
                n += 1
        print(f"    {len(zf.namelist())} files extracted (~{len(data) / 1e6:.1f} MB)")

    elif entry["type"] == "image":
        (study / entry["out"]).write_bytes(_get(entry["url"]))
        if entry.get("label_url"):
            truth.mkdir(parents=True, exist_ok=True)
            (truth / entry["label_out"]).write_bytes(_get(entry["label_url"]))
        print(f"    image saved ({(study / entry['out']).stat().st_size / 1e6:.1f} MB)")

    elif entry["type"] == "msd_image":
        data = _get(entry["tar_url"])
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            img = tf.extractfile(entry["image_member"])
            (study / entry["out"]).write_bytes(img.read())
            if entry.get("label_member"):
                truth.mkdir(parents=True, exist_ok=True)
                lab = tf.extractfile(entry["label_member"])
                (truth / entry["label_out"]).write_bytes(lab.read())
        print(f"    extracted {entry['out']} from {len(data) / 1e6:.0f} MB tar")

    return {
        "path": f"test_data/labeled/{name}/study",
        "name": name,
        "anatomy": entry["anatomy"],
        "modality": entry["modality"],
        "calibrated": None,
        "diagnosis": entry["diagnosis"],
        "expect_findings": entry["expect_findings"],
        "expect_absent": entry["expect_absent"],
    }


def main():
    LABELED.mkdir(parents=True, exist_ok=True)
    studies, failed = [], []
    for entry in MANIFEST:
        try:
            studies.append(fetch(entry))
        except Exception as e:  # noqa: BLE001
            failed.append(entry["name"])
            print(f"    [FAILED] {e}")
    out = HERE / "ground_truth_labeled.json"
    out.write_text(json.dumps({
        "_help": "Auto-generated by fetch_labeled.py — labeled public studies with known diagnoses. "
                 "Merged with ground_truth.json by validate.py.",
        "studies": studies,
    }, indent=2), encoding="utf-8")
    print(f"\nFetched {len(studies)}/{len(MANIFEST)} studies -> {out.relative_to(REPO)}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
