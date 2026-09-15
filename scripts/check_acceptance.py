"""Check that every design acceptance ID has passing, non-skipped JUnit evidence."""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("junit")
parser.add_argument("--output", default="acceptance-report.json")
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
spec = (root / "ProtocolLab_Design_Spec_v0.1_vi.md").read_text()
required = set(re.findall(r"\| ([BLMPGREO]\d{2}) \|", spec))
mapping = json.loads((root / "docs" / "acceptance-matrix.json").read_text())
tree = ET.parse(args.junit)
cases = {}
for case in tree.iter("testcase"):
    name = case.attrib["name"].split("[")[0]
    passed = not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
    cases[name] = cases.get(name, True) and passed
results = {key: bool(mapping.get(key)) and all(cases.get(name, False) for name in mapping.get(key, [])) for key in sorted(required)}
report = {"spec_acceptance_ids": len(required), "passed": sum(results.values()), "results": results,
          "status": "PASS" if all(results.values()) and set(mapping) == required else "FAIL",
          "scope": "Engineering acceptance matrix. Does not establish H1-H5 research gates."}
Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
raise SystemExit(report["status"] != "PASS")
