"""Per-commit view of the selector on the sample set: misses, cost, kind.

Usage: python3 tools/eval.py [sample_dir]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import selector  # noqa: E402

sample = Path(sys.argv[1] if len(sys.argv) > 1 else "sample")
tests = json.loads((sample / "tests.json").read_text())["tests"]
answers = json.loads((sample / "answers.json").read_text())["commits"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
sel = selector.Selector(selector.MAP, tests)

total = misses = 0
for patch in sorted((sample / "commits").glob("*.patch")):
    cid = patch.stem
    chosen, notes = sel.select(patch.read_text())
    _, cost = selector.pack(chosen, ms)
    failing = set(answers[cid]["failing"])
    missed = failing - chosen
    total += cost + (300000 if missed else 0)
    misses += bool(missed)
    flag = "MISS" if missed else "    "
    print(f"{flag} {cid} {answers[cid]['kind']:8} fail={len(failing):2} sel={len(chosen):4} cost={cost/1000:6.1f}s {' '.join(notes)}")
    if missed:
        print("      missed:", [tests[n]["id"] for n in sorted(missed)][:5])
print(f"\nsimulated total {total/1000:.1f}s, misses {misses}")
