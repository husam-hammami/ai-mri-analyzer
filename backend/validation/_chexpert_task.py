"""Scheduled-task runner for the CheXpert benchmark — survives app close.
Loops the (cache-resuming) eval until all 50 studies are read, then self-deletes the task.
Replicates the working shell env (puts npm on PATH so the `claude` CLI resolves) + high effort.
"""
import glob
import os
import subprocess
import sys
import time

BACKEND = r"C:\Users\husam\OneDrive\Documents\MRI_Analayis_AI\backend"
CACHE = os.path.join(BACKEND, "validation", "cache_chexpert")
LOG = os.path.join(BACKEND, "validation", "chexpert_task.log")
TARGET = 50

os.environ["MIKA_AGENT_EFFORT"] = "high"
os.environ["PATH"] = r"C:\Users\husam\AppData\Roaming\npm" + os.pathsep + os.environ.get("PATH", "")


def cached():
    return len(glob.glob(os.path.join(CACHE, "*", "summary.json")))


def main():
    with open(LOG, "a", encoding="utf-8") as lf:
        lf.write(f"\n=== task fire | cached={cached()}/{TARGET} ===\n")
        lf.flush()
        for _ in range(40):
            if cached() >= TARGET:
                break
            subprocess.run([sys.executable, "-u", "-m", "validation.chexpert_eval", "--limit", str(TARGET)],
                           cwd=BACKEND, stdout=lf, stderr=lf)
            lf.write(f"-- pass complete | cached={cached()}/{TARGET} --\n")
            lf.flush()
            time.sleep(3)
        if cached() >= TARGET:
            lf.write("ALL DONE — deleting scheduled task\n")
            lf.flush()
            subprocess.run(["schtasks", "/delete", "/tn", "MIKA_CheXpert", "/f"], capture_output=True)


if __name__ == "__main__":
    main()
