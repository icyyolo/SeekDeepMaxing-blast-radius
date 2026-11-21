"""Blast Radius selector: pick the tests a sqlglot patch can break.

Uses maps/map.json.gz, built by tools/collect.sh + tools/build_maps.py from
running sqlglot's own test suite at the pinned commit:
  - per-line coverage: which tests execute each source line
  - function spans: so a change anywhere in a function selects every test
    that runs any line of that function
  - file reads: which tests open each fixture/data file

Changes outside any function (module level, class bodies, signatures of
deleted code) fall back to every test that covers any line of that file.
Unknown files fall back to running everything. Then the selected tests are
packed onto the number of workers that minimizes busiest-worker time plus
2 s per worker.
"""

import bisect
import gzip
import json
import re
import sys
from pathlib import Path

DATA = Path("/data")
MAP = Path(__file__).resolve().parent / "maps" / "map.json.gz"
WORKER_COST_MS = 2000
MAX_WORKERS = 8

# Paths that cannot change test outcomes.
INERT = re.compile(
    r"^(docs/|posts/|benchmarks/|pdoc/|\.github/|README|CHANGELOG|CONTRIBUTING|LICENSE|AGENTS\.md|CLAUDE\.md|sqlglot\.png)"
)
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def log(*args):
    print(*args, file=sys.stderr)


def parse_patch(text):
    """Return {old_path: {"new": new_path, "lines": set(old line numbers touched)}}."""
    files = {}
    cur = None
    old_line = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = None
            continue
        if line.startswith("--- "):
            old = line[4:].split("\t")[0]
            pending_old = None if old == "/dev/null" else old[2:] if old.startswith("a/") else old
            continue
        if line.startswith("+++ "):
            new = line[4:].split("\t")[0]
            new = None if new == "/dev/null" else new[2:] if new.startswith("b/") else new
            key = pending_old if pending_old is not None else new
            cur = files.setdefault(key, {"old": pending_old, "new": new, "lines": set()})
            continue
        m = HUNK.match(line)
        if m and cur is not None:
            old_line = int(m.group(1))
            if m.group(2) == "0":
                # Pure insertion after line old_line.
                cur["lines"].update((old_line, old_line + 1))
                old_line += 1
            continue
        if cur is None or not line:
            if cur is not None and not line:
                old_line += 1
            continue
        tag = line[0]
        if tag == "-":
            cur["lines"].add(old_line)
            old_line += 1
        elif tag == "+":
            cur["lines"].update((old_line - 1, old_line))
        elif tag == " ":
            old_line += 1
    return files


class Selector:
    def __init__(self, map_path, tests):
        with gzip.open(map_path, "rt") as f:
            m = json.load(f)
        number_of = {t["id"]: t["number"] for t in tests}
        # Map indices in the map to this run's test numbers.
        self.idx_to_num = [number_of.get(tid) for tid in m["test_ids"]]
        self.all_tests = {t["number"] for t in tests}
        self.classes = m["classes"]
        self.files = m["files"]
        self.reads = m["reads"]
        self.uses = m["uses"]
        self.tests_by_file = {}
        for t in tests:
            self.tests_by_file.setdefault(t["file"], set()).add(t["number"])
        self._file_cache = {}

    def _nums(self, indices):
        out = set()
        for i in indices:
            if i is not None and 0 <= i < len(self.idx_to_num):
                n = self.idx_to_num[i]
                if n is not None:
                    out.add(n)
        return out

    def _lines_tests(self, entry, lo, hi):
        cids = set()
        for ln, cid in entry["lines"].items():
            if lo <= int(ln) <= hi:
                cids.add(cid)
        out = set()
        for cid in cids:
            out.update(self.classes[cid])
        return out

    def _file_tests(self, path):
        """Every test that runs a line of the file or uses a class defined in it."""
        if path not in self._file_cache:
            entry = self.files[path]
            out = self._lines_tests(entry, 0, 10**9)
            for _s, _e, qual in entry.get("classes", ()):
                out.update(self.uses.get(f"{path}::{qual}", ()))
            self._file_cache[path] = self._nums(out) | self.tests_by_file.get(path, set())
        return self._file_cache[path]

    @staticmethod
    def _innermost(spans, line):
        best = None
        for span in spans:
            if span[0] <= line <= span[1] and (best is None or span[0] >= best[0]):
                best = span
        return best

    def for_py(self, path, lines):
        entry = self.files.get(path)
        if entry is None:
            return None  # unknown python file
        selected = set()
        for line in sorted(lines):
            func = self._innermost(entry.get("funcs", ()), line)
            if func is not None:
                selected |= self._nums(self._lines_tests(entry, func[0], func[1]))
                continue
            klass = self._innermost(entry.get("classes", ()), line)
            key = klass and f"{path}::{klass[2]}"
            if klass is None or key not in self.uses:
                # Module level, or a class no test instantiated: whole file.
                return self._file_tests(path)
            # Class body (attributes, tables): tests that use the class or a
            # subclass, plus tests that run any of its methods.
            selected |= self._nums(self.uses[key])
            selected |= self._nums(self._lines_tests(entry, klass[0], klass[1]))
        return selected

    def for_path(self, info):
        path = info["old"] or info["new"]
        if INERT.match(path):
            return set(), "inert"
        if info["old"] is None:
            # New file: existing tests cannot reach it unless another changed
            # file imports it, which that file's own change will select.
            if path.startswith("tests/") and not path.endswith(".py"):
                return set(), "new-data"
            return set(), "new-file"
        if path.endswith(".py"):
            if info["new"] is None:
                return self._file_tests(path) if path in self.files else None, "deleted"
            got = self.for_py(path, info["lines"])
            return got, "py"
        if path in self.reads:
            readers = self.reads[path]
            if -1 in readers:
                return None, "read-at-collect"
            return self._nums(readers), "read"
        if path.startswith("tests/fixtures/"):
            return None, "unread-fixture"
        return None, "unknown"

    def select(self, patch_text):
        selected = set()
        notes = []
        for info in parse_patch(patch_text).values():
            got, why = self.for_path(info)
            notes.append(f"{info['old'] or info['new']}:{why}")
            if got is None:
                return set(self.all_tests), notes + ["ALL"]
            selected |= got
        return selected, notes


def pack(numbers, ms):
    """Best worker count by LPT; returns (workers, cost_ms)."""
    if not numbers:
        return [], 0
    order = sorted(numbers, key=lambda n: ms[n], reverse=True)
    best = None
    for w in range(1, MAX_WORKERS + 1):
        if w > len(order):
            break
        workers = [[] for _ in range(w)]
        loads = [0] * w
        for n in order:
            i = loads.index(min(loads))
            workers[i].append(n)
            loads[i] += ms[n]
        cost = max(loads) + WORKER_COST_MS * w
        if best is None or cost < best[1]:
            best = (workers, cost)
    return best


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    sel = Selector(MAP, tests)
    total = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        chosen, notes = sel.select(patch.read_text(encoding="utf-8", errors="replace"))
        workers, cost = pack(chosen, ms)
        total += cost
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        log(f"{patch.stem}: {len(chosen)} tests, {cost / 1000:.1f}s {' '.join(notes)}")
    log(f"total simulated (no misses counted): {total / 1000:.1f}s")


if __name__ == "__main__":
    main()
