"""One-off: read the design-second-reader workflow output and emit second_reader_prompt.py."""
import json
import importlib.util

OUT = (r"C:\Users\husam\AppData\Local\Temp\claude\C--Users-husam-OneDrive-Documents-MRI-Analayis-AI"
       r"\ddc98ac1-d928-4f7c-9850-87c1922f2d3a\tasks\wiritpv6a.output")

d = json.load(open(OUT, encoding="utf-8"))["result"]
lines = [
    '"""Second-reader sensitivity-pass prompt — produced by the design-second-reader workflow.',
    "Two-gate decision: relative focal-outlier (sensitivity) + same-location corroboration (specificity),",
    'with a normal anchor + hard normal-guard. Consumed by second_reader.py."""',
    "",
    "TEMPLATE = " + repr(d["template"]),
    "",
    "HUNT_BLOCKS = {",
    "    'prostate': " + repr(d["prostate_hunt"]) + ",",
    "    'abdomen': " + repr(d["abdomen_hunt"]) + ",",
    "    'generic': " + repr(d["generic_hunt"]) + ",",
    "}",
    "",
]
open("second_reader_prompt.py", "w", encoding="utf-8").write("\n".join(lines))
print("wrote second_reader_prompt.py")

spec = importlib.util.spec_from_file_location("srp", "second_reader_prompt.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
rendered = m.TEMPLATE.format(anatomy="prostate", modality="MR", study_dir="X", out_dir="Y",
                            primary_conclusion="Z", hunt_block=m.HUNT_BLOCKS["prostate"])
print("format() OK | rendered prompt chars:", len(rendered), "| hunt blocks:", list(m.HUNT_BLOCKS))
