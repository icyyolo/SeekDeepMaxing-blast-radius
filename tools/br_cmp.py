"""pytest plugin used only while building maps (never in the scored image).

Records, per test, the comparisons in sqlglot whose result would change if
the operator were swapped for its near twin:

    is <-> ==    is not <-> !=    < <-> <=    > <-> >=

Every single-operator comparison in sqlglot/ is rewritten at import time into
a call that computes the original result (returned unchanged, same evaluation
order) and the twin's result. If the two differ (or the twin raises, or a
result is not a plain bool), the comparison "diverges" in the running test.
A patch that swaps one such operator can only change the behavior of tests
in which that comparison diverged; in every other test each evaluation gives
the same value as before.

Load together with br_trace (which tracks the current test, runs process
pools in-process and imports every module up front):
    pytest -p br_cmp -p br_trace ...
Output: $BR_OUT/cmp-<pid>.json
    {"sites": [[path, line, col, op], ...],
     "ran": [site ids executed at least once],
     "diverged": {context: [site ids]}}
"""
import ast
import builtins
import importlib.abc
import importlib.machinery
import io
import json
import operator
import os
import sys
import tokenize

ROOT = os.path.realpath(os.environ.get("BR_ROOT", os.getcwd()))
PKG = os.path.join(ROOT, "sqlglot") + os.sep
OUT = os.environ["BR_OUT"]

OPS = {
    "Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne,
    "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge,
}
TWIN = {"Is": "Eq", "Eq": "Is", "IsNot": "NotEq", "NotEq": "IsNot",
        "Lt": "LtE", "LtE": "Lt", "Gt": "GtE", "GtE": "Gt"}
TOKEN_OPS = {"is", "==", "!=", "<", "<=", ">", ">=", "in", "not"}

_sites = []  # site id -> [path, line, col, op]
_ran = set()
_diverged = {}  # context -> set of site ids
_current = None  # br_trace._current, bound lazily


def _context():
    global _current
    if _current is None:
        _current = sys.modules["br_trace"]._current
    return _current[0]


_Expr = None


def _hashes_tree(op, left, right):
    """True if the twin would be Expr == Expr on two distinct nodes of the same
    type: that hashes both trees and caches the hash on every node, a side
    effect the original run never had (and slow). Count it as diverging
    without evaluating it."""
    global _Expr
    if op not in ("Is", "IsNot") or left is right or type(left) is not type(right):
        return False
    if _Expr is None:
        core = sys.modules.get("sqlglot.expressions.core")
        if core is None:
            return True
        _Expr = core.Expr
    return isinstance(left, _Expr)


def _br_cmp(site, left, op, right):
    result = OPS[op](left, right)  # original; raises exactly as before
    _ran.add(site)
    if _hashes_tree(op, left, right):
        same = False
    else:
        try:
            twin = OPS[TWIN[op]](left, right)
            same = type(result) is bool and type(twin) is bool and result == twin
        except Exception:
            same = False
    if not same:
        _diverged.setdefault(_context(), set()).add(site)
    return result


builtins._br_cmp = _br_cmp


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel, text):
        self.rel = rel
        self.lines = text.splitlines(True)
        self.ops = []  # (row, char col, byte col) of comparison operator tokens
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.OP, tokenize.NAME) and tok.string in TOKEN_OPS:
                row, col = tok.start
                self.ops.append((row, col, len(self.lines[row - 1][:col].encode())))

    def _op_token(self, node):
        lo = (node.left.end_lineno, node.left.end_col_offset)
        hi = (node.comparators[0].lineno, node.comparators[0].col_offset)
        found = [(r, c) for r, c, b in self.ops if lo <= (r, b) < hi]
        return found[0] if found else None

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        op = type(node.ops[0]).__name__
        if op not in TWIN:
            return node
        pos = self._op_token(node)
        if pos is None:
            return node
        site = len(_sites)
        _sites.append([self.rel, pos[0], pos[1], op])
        call = ast.Call(
            func=ast.Name("_br_cmp", ast.Load()),
            args=[ast.Constant(site), node.left, ast.Constant(op), node.comparators[0]],
            keywords=[],
        )
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):  # always from source, never a cached .pyc
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def source_to_code(self, data, path, *, _optimize=-1):
        text = data.decode("utf-8")
        rel = os.path.relpath(os.path.realpath(path), ROOT)
        tree = ast.parse(text, path)
        tree = ast.fix_missing_locations(_Rewriter(rel, text).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.origin and os.path.realpath(spec.origin).startswith(PKG) and spec.origin.endswith(".py"):
            spec.loader = _Loader(fullname, spec.origin)
        return spec


assert "sqlglot" not in sys.modules, "br_cmp must load before sqlglot is imported"
sys.meta_path.insert(0, _Finder())
sys.dont_write_bytecode = True


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"cmp-{os.getpid()}.json"), "w") as f:
        json.dump(
            {"sites": _sites, "ran": sorted(_ran),
             "diverged": {k: sorted(v) for k, v in _diverged.items()}},
            f,
        )
