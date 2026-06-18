"""Print how many ground-truth studies still lack a cached read (0 = benchmark complete).
Used by run_until_done.ps1 to auto-resume the reading benchmark until everything is done."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    studies = json.loads((HERE / "ground_truth.json").read_text(encoding="utf-8")).get("studies", [])
    gl = HERE / "ground_truth_labeled.json"
    if gl.exists():
        studies += json.loads(gl.read_text(encoding="utf-8")).get("studies", [])
    missing = []
    for s in studies:
        if not (s.get("expect_findings") or s.get("diagnosis")):
            continue  # only studies we can score get read
        name = s.get("name") or Path(s["path"]).name
        if not (HERE / "cache" / name / "summary.json").exists():
            missing.append(name)
    print(len(missing))
    if missing:
        sys.stderr.write("remaining: " + ", ".join(missing) + "\n")


if __name__ == "__main__":
    main()
