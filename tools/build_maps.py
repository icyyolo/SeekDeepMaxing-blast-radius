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

# Function spans (innermost lookup happens at run time), class spans,
# compound-statement spans inside functions, class-body assignments, and
# where each name is read (name -> {(file, enclosing function start)};
# function start 0 means read outside any function, i.e. at import).
BLOCKS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)
refs_raw = {}


def ref_name(node):
    """The name an expression reads: x.NAME, NAME, or "NAME" (for getattr)."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


for path in glob.glob(os.path.join(SRC, "sqlglot", "**", "*.py"), recursive=True) + glob.glob(
    os.path.join(SRC, "tests", "**", "*.py"), recursive=True
):
    rel = os.path.relpath(path, SRC)
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError:
        continue
    funcs, klasses, blocks, assigns, strings = [], [], [], [], []

    def visit(node, qual, func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                name = f"{qual}.{child.name}" if qual else child.name
                if isinstance(child, ast.ClassDef):
                    klasses.append([start, child.end_lineno, name])
                    for stmt in child.body:
                        targets = []
                        if isinstance(stmt, ast.Assign):
                            targets = stmt.targets
                        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                            targets = [stmt.target]
                        names = [t.id for t in targets if isinstance(t, ast.Name)]
                        if names and len(names) == len(targets):
                            assigns.append([stmt.lineno, stmt.end_lineno, names])
                    visit(child, name, None)
                else:
                    span = [start, child.end_lineno, child.body[0].lineno]
                    funcs.append(span)
                    visit(child, name + ".<locals>", span)
                continue
            if func is not None and isinstance(child, BLOCKS):
                blocks.append([child.lineno, child.end_lineno])
            if isinstance(child, (ast.Constant, ast.JoinedStr)) and child.end_lineno > child.lineno:
                strings.append([child.lineno, child.end_lineno])
            name = ref_name(child)
            if name is not None:
                # Function start 0: read outside any function (module level or
                # a class body), i.e. at import time.
                refs_raw.setdefault(name, set()).add((rel, func[0] if func else 0))
            visit(child, qual, func)

    visit(tree, "", None)
    entry = files.setdefault(rel, {"lines": {}, "import_lines": []})
    entry["funcs"] = sorted(funcs)
    entry["classes"] = sorted(klasses)
    entry["blocks"] = sorted(blocks)
    entry["assigns"] = sorted(assigns)
    entry["strings"] = sorted(strings)

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

# Static import graph: importers[m] = files that import module m anywhere
# (top level or inside functions). Used to find what depends on a module's
# import-time values, which per-test coverage cannot attribute.
def module_file(name):
    base = os.path.join(SRC, *name.split("."))
    for cand in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(cand):
            return os.path.relpath(cand, SRC)
    return None


def prefixes(name):
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


importers = {}
for path in glob.glob(os.path.join(SRC, "sqlglot", "**", "*.py"), recursive=True) + glob.glob(
    os.path.join(SRC, "tests", "**", "*.py"), recursive=True
):
    rel = os.path.relpath(path, SRC)
    pkg_parts = os.path.dirname(rel).replace(os.sep, ".").split(".")
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError:
        continue
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(prefixes(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - node.level + 1]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if not mod:
                continue
            names.update(prefixes(mod))
            for alias in node.names:
                names.add(f"{mod}.{alias.name}")
    for name in names:
        target = module_file(name)
        if target and target != rel:
            importers.setdefault(target, set()).add(rel)

assigned = {n for e in files.values() for _s, _e, names in e.get("assigns", ()) for n in names}
refs = {n: sorted(refs_raw.get(n, ())) for n in assigned}

class_list = [None] * len(classes)
for key, cid in classes.items():
    class_list[cid] = sorted(key)

out = {
    "test_ids": test_ids,
    "classes": class_list,
    "files": files,
    "reads": {f: sorted(s) for f, s in reads.items()},
    "uses": {k: sorted(s) for k, s in uses.items()},
    "importers": {k: sorted(s) for k, s in importers.items()},
    "refs": refs,
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
