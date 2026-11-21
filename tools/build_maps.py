"""Turn the raw coverage + read traces from collect.sh into maps/map.json.gz.

Everything here is derived from running sqlglot's own tests at the pinned
commit (coverage contexts and file reads) plus the AST of its source files.
Nothing is derived from patches or answers.

Usage (inside the br-map image):
  python tools/build_maps.py /src /raw sample/tests.json maps/map.json.gz
"""
import ast
import glob
import gzip
import json
import os
import sys

from coverage import CoverageData

SRC, RAW, TESTS_JSON, OUT = sys.argv[1:5]

test_ids = [t["id"] for t in json.load(open(TESTS_JSON))["tests"]]
index = {tid: i for i, tid in enumerate(test_ids)}


def test_of(context):
    """'tests/x.py::C::t|run' -> index, or None for import-time contexts."""
    nodeid = context.split("|")[0]
    return index.get(nodeid)


data = CoverageData(basename=os.path.join(RAW, ".coverage"))
data.read()

classes = {}  # frozenset of test indices -> class id
files = {}
unknown_contexts = set()

for abs_path in data.measured_files():
    rel = os.path.relpath(abs_path, SRC)
    if rel.startswith(".."):
        continue
    by_line = data.contexts_by_lineno(abs_path)
    lines = {}
    import_lines = []
    for lineno, contexts in by_line.items():
        tests = set()
        for ctx in contexts:
            if not ctx:
                continue
            i = test_of(ctx)
            if i is None:
                unknown_contexts.add(ctx.split("|")[0])
            else:
                tests.add(i)
        if "" in contexts:
            import_lines.append(lineno)
        if tests:
            key = frozenset(tests)
            lines[lineno] = classes.setdefault(key, len(classes))
    files[rel] = {"lines": lines, "import_lines": sorted(import_lines)}

# Function spans (innermost lookup happens at run time) and class spans.
for path in glob.glob(os.path.join(SRC, "sqlglot", "**", "*.py"), recursive=True) + glob.glob(
    os.path.join(SRC, "tests", "**", "*.py"), recursive=True
):
    rel = os.path.relpath(path, SRC)
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError:
        continue
    funcs, klasses = [], []

    def visit(node, qual):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                name = f"{qual}.{child.name}" if qual else child.name
                if isinstance(child, ast.ClassDef):
                    klasses.append([start, child.end_lineno, name])
                else:
                    funcs.append([start, child.end_lineno, child.body[0].lineno])
                    name += ".<locals>"
                visit(child, name)
            else:
                visit(child, qual)

    visit(tree, "")
    entry = files.setdefault(rel, {"lines": {}, "import_lines": []})
    entry["funcs"] = sorted(funcs)
    entry["classes"] = sorted(klasses)

# File reads (fixtures and other data files) per test.
reads = {}
for path in glob.glob(os.path.join(RAW, "reads-*.json")):
    for ctx, read_files in json.load(open(path)).items():
        i = index.get(ctx)
        for f in read_files:
            reads.setdefault(f, set())
            if i is None:
                reads[f].add(-1)  # read outside any test (collection time)
            else:
                reads[f].add(i)

# Classes each test instantiated (with MRO), keyed "path::Qualname".
uses = {}
for path in glob.glob(os.path.join(RAW, "uses-*.json")):
    for ctx, keys in json.load(open(path)).items():
        i = index.get(ctx)
        for key in keys:
            uses.setdefault(key, set()).add(-1 if i is None else i)

class_list = [None] * len(classes)
for key, cid in classes.items():
    class_list[cid] = sorted(key)

out = {
    "test_ids": test_ids,
    "classes": class_list,
    "files": files,
    "reads": {f: sorted(s) for f, s in reads.items()},
    "uses": {k: sorted(s) for k, s in uses.items()},
}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with gzip.open(OUT, "wt") as f:
    json.dump(out, f, separators=(",", ":"))

covered = set()
for e in files.values():
    for cid in e["lines"].values():
        covered.update(class_list[cid])
print(
    f"files={len(files)} classes={len(class_list)} reads={len(reads)} "
    f"tests_with_coverage={len(covered)}/{len(test_ids)} unknown_contexts={len(unknown_contexts)} "
    f"size={os.path.getsize(OUT)} bytes",
    file=sys.stderr,
)
if unknown_contexts:
    print("unknown:", sorted(unknown_contexts)[:10], file=sys.stderr)
