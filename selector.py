"""Blast Radius selector: pick the tests a sqlglot patch can break.

Uses maps/map.json.gz, built by tools/collect.sh + tools/build_maps.py from
running sqlglot's own test suite at the pinned commit:
  - per-line coverage: which tests execute each source line
  - function and compound-statement spans (if/for/try/...): a changed line
    inside a function selects every test that runs any line of the
    innermost compound statement around it (or of the function)
  - file reads: which tests open each fixture/data file, or read a source
    file as text
  - class usage: which tests instantiate each class (with its MRO)
  - import lines, a static import graph, multi-line string spans, and where
    each class attribute name is read

Comment and blank lines are ignored unless they sit inside a multi-line
string. A class attribute change selects the tests that use the class and
run a function reading that name; if the name is read at import time, it
falls back to every test that uses the class. A module-level change selects
the file tests of that module and of every module importing it. A change to
a function that runs at import time (metaclasses, table builders), a deleted
or renamed module, or an unknown file selects everything. Then the selected
tests are packed onto the number of workers that minimizes busiest-worker
time plus 2 s per worker.
"""

import bisect
import gzip
import json
import os
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
# Safety net, off by default. A comma list of kinds of changed file that get
# a wider selection than the maps give, trading time for fewer misses:
#   data    - a non-Python file (fixtures, configs): run every test
#   comment - a Python file where only comment or blank lines changed: run every test
#   code    - a Python code change: run every test
#   wide    - a Python code change: every test of the module and of every
#             module importing it, plus tests that read it as text
#   all     - run every test on every commit
SAFE = set(filter(None, os.environ.get("BR_SAFE", "code").split(",")))
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF = re.compile(r"^diff --git a/(.+) b/(.+)$")


def log(*args):
    print(*args, file=sys.stderr)


def _is_comment(code):
    s = code.strip()
    return not s or s.startswith("#")


def parse_patch(text):
    """Return {old_path: {"old", "new", "lines": set(old line numbers touched)}}.

    Files are registered from the "diff --git" header, so renames, mode
    changes and binary diffs (which have no ---/+++ lines) are not dropped.
    ---/+++ lines are only read as headers before the first hunk of a file, so
    a removed SQL comment line ("--- x") inside a hunk is not mistaken for one.
    """
    files = {}
    cur = None
    in_header = True
    pending_old = None
    old_line = 0
    is_py = False
    replacing = False
    for line in text.splitlines():
        m = DIFF.match(line)
        if m:
            old, new = m.groups()
            cur = files.setdefault(old, {"old": old, "new": new, "lines": set(), "comments": set()})
            in_header = True
            continue
        if in_header and cur is not None:
            if line.startswith("new file mode"):
                cur["old"] = None
            elif line.startswith("deleted file mode"):
                cur["new"] = None
        if line.startswith("--- ") and (cur is None or in_header):
            old = line[4:].split("\t")[0]
            pending_old = None if old == "/dev/null" else old[2:] if old.startswith("a/") else old
            continue
        if line.startswith("+++ ") and (cur is None or in_header):
            new = line[4:].split("\t")[0]
            new = None if new == "/dev/null" else new[2:] if new.startswith("b/") else new
            if cur is None:  # plain unified diff without a "diff --git" header
                key = pending_old if pending_old is not None else new
                cur = files.setdefault(key, {"old": pending_old, "new": new, "lines": set(), "comments": set()})
            else:
                cur["old"], cur["new"] = pending_old, new
            continue
        m = HUNK.match(line)
        if m and cur is not None:
            in_header = False
            old_line = int(m.group(1))
            is_py = (cur["new"] or cur["old"] or "").endswith(".py")
            replacing = False
            if m.group(2) == "0":
                old_line += 1  # pure insertion after line old_line
            continue
        if cur is None or not line:
            if cur is not None and not line:
                old_line += 1
                replacing = False
            continue
        tag = line[0]
        if tag in "+-" and is_py and _is_comment(line[1:]):
            # A Python comment or blank line cannot change behavior unless it
            # sits inside a multi-line string; record where it is so the
            # selector can check that against the string spans in the map.
            if tag == "-":
                cur["comments"].add((old_line, old_line))
                old_line += 1
            else:
                cur["comments"].add((old_line - 1, old_line))
            continue
        if tag == "-":
            cur["lines"].add(old_line)
            old_line += 1
            replacing = True
        elif tag == "+":
            # Lines that replace removed lines are covered by those; a pure
            # insertion touches the lines on both sides of it.
            if not replacing:
                cur["lines"].update((old_line - 1, old_line))
        elif tag == " ":
            old_line += 1
            replacing = False
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
        self.importers = m.get("importers", {})
        self.refs = m.get("refs", {})
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

    def _module_tests(self, path):
        """A module-level change: values computed at import time can reach any
        module that imports this one, so take the file tests of this module
        and of every module that imports it, transitively."""
        seen = {path}
        todo = [path]
        while todo:
            for imp in self.importers.get(todo.pop(), ()):
                if imp not in seen:
                    seen.add(imp)
                    todo.append(imp)
        out = set()
        for p in seen:
            if p in self.files:
                out |= self._file_tests(p)
            out |= self.tests_by_file.get(p, set())
        return out

    @staticmethod
    def _runs_at_import(entry, func):
        """True if the function's body ran while modules were being imported
        (metaclasses, table builders called from class bodies). Coverage
        credits that to no test, so its effects cannot be traced per test."""
        imp = entry.get("import_lines", ())
        return any(func[2] <= ln <= func[1] for ln in imp)

    def _attr_tests(self, names, key):
        """A class attribute (NAME = ...) only matters where NAME is read.
        Tests that use the class and run a function reading NAME, plus test
        functions that read it directly. None (fall back) if NAME is read at
        import time, where its effect cannot be traced per test."""
        sqlglot_side, test_side = set(), set()
        for name in names:
            if not self.refs.get(name):
                return None  # never read by name: maybe read dynamically
            for path, start in self.refs[name]:
                entry = self.files.get(path)
                func = entry and next((f for f in entry.get("funcs", ()) if f[0] == start), None)
                if func is None or self._runs_at_import(entry, func):
                    return None
                got = self._lines_tests(entry, func[0], func[1])
                (test_side if path.startswith("tests/") else sqlglot_side).update(got)
        return (self._nums(self.uses[key]) & self._nums(sqlglot_side)) | self._nums(test_side)

    @staticmethod
    def _innermost(spans, line):
        best = None
        for span in spans:
            if span[0] <= line <= span[1] and (best is None or span[0] >= best[0]):
                best = span
        return best

    def _in_strings(self, path, comments):
        """Comment/blank lines that fall inside a multi-line string literal
        change that string's value, so they count as changed lines."""
        spans = self.files.get(path, {}).get("strings", ())
        out = set()
        for a, b in comments:
            if any(s <= a and b <= e for s, e in spans):
                out.update((a, b))
        return out

    def for_py(self, path, lines):
        """Returns (tests or None for all, note)."""
        entry = self.files.get(path)
        if entry is None:
            return None, "unknown-py"
        selected = set()
        why = "py"
        for line in sorted(lines):
            func = self._innermost(entry.get("funcs", ()), line)
            if func is not None:
                if self._runs_at_import(entry, func):
                    return None, "import-time-func"
                # A changed line only affects runs that reach it, and every
                # such run executes some line of the innermost compound
                # statement (if/for/try/...) around it, else of the function.
                inner = [b for b in entry.get("blocks", ()) if b[0] >= func[2] and b[1] <= func[1]]
                span = self._innermost(inner, line) if line >= func[2] else None
                lo, hi = span or func[:2]
                selected |= self._nums(self._lines_tests(entry, lo, hi))
                continue
            klass = self._innermost(entry.get("classes", ()), line)
            key = klass and f"{path}::{klass[2]}"
            if klass is None or key not in self.uses:
                # Module level, or a class no test instantiated.
                return self._module_tests(path), "module"
            assign = self._innermost(entry.get("assigns", ()), line)
            got = self._attr_tests(assign[2], key) if assign else None
            if got is not None:
                selected |= got
                why = "attr"
                continue
            # Class body (attributes, tables): tests that use the class or a
            # subclass, plus tests that run any of its methods.
            selected |= self._nums(self.uses[key])
            selected |= self._nums(self._lines_tests(entry, klass[0], klass[1]))
            why = "class"
        return selected, why

    def for_path(self, info):
        path = info["old"] or info["new"]
        if INERT.match(path):
            return set(), "inert"
        if info["old"] is None:
            name = path.rsplit("/", 1)[-1]
            if name == "conftest.py" or (path.startswith("tests/") and name == "__init__.py"):
                return None, "new-conftest"
            # New file: existing tests cannot reach it unless another changed
            # file imports it, which that file's own change will select, or a
            # test lists its package and reads the sources it finds
            # (test_typing scans sqlglot/typing/), so take the tests that read
            # its sibling modules as text.
            if path.startswith("tests/") and not path.endswith(".py"):
                return set(), "new-data"
            folder = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
            siblings = set()
            if path.endswith(".py"):
                for other, readers in self.reads.items():
                    rest = other[len(folder):]
                    if other.startswith(folder) and "/" not in rest and rest.endswith(".py"):
                        siblings.update(r for r in readers if r >= 0)
            return self._nums(siblings), "new-file"
        if path.endswith(".py"):
            if info["new"] is None or info["new"] != info["old"]:
                # Deleted or renamed module: every importer breaks.
                return None, "deleted-or-renamed"
            got, why = self.for_py(path, info["lines"] | self._in_strings(path, info["comments"]))
            # Tests that read this source file as text (not by importing it).
            readers = [r for r in self.reads.get(path, ()) if r >= 0]
            if got is not None and readers:
                got = got | self._nums(readers)
                why += "+read"
            return got, why
        if path in self.reads:
            readers = self.reads[path]
            if -1 in readers:
                return None, "read-at-collect"
            return self._nums(readers), "read"
        if path.startswith("tests/fixtures/"):
            return None, "unread-fixture"
        return None, "unknown"

    @staticmethod
    def kind_of(info):
        path = info["old"] or info["new"]
        if INERT.match(path):
            return "inert"
        if not path.endswith(".py"):
            return "data"
        if info["lines"] or info["old"] is None or info["new"] != info["old"]:
            return "code"
        return "comment"

    def safe_path(self, info):
        """The SAFE widening for one changed file: (tests or None for all, note),
        or False when SAFE does not apply to it."""
        kind = self.kind_of(info)
        if kind == "inert":
            return False
        if "all" in SAFE or kind in SAFE:
            return None, f"safe-{kind}"
        if kind == "code" and "wide" in SAFE:
            path = info["old"] or info["new"]
            if info["old"] is None or path not in self.files:
                return None, "safe-wide"
            got = self._module_tests(path) | self._nums(r for r in self.reads.get(path, ()) if r >= 0)
            return got, "safe-wide"
        return False

    def select(self, patch_text):
        selected = set()
        notes = []
        for info in parse_patch(patch_text).values():
            got, why = self.for_path(info)
            safe = self.safe_path(info) if SAFE else False
            if safe is not False:
                wide, why = safe
                got = None if wide is None or got is None else got | wide
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
