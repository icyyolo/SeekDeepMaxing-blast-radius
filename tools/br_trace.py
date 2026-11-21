"""pytest plugin used only while building maps (never in the scored image).

Per test, records:
  - reads: non-Python files under the sqlglot checkout the test opens, so
    edits to tests/fixtures/*.sql map to the tests that read them.
  - uses: sqlglot classes the test instantiates (Dialect, Parser, Generator,
    Tokenizer, Expr subclasses, with their whole MRO), so an edit to a class
    attribute (e.g. TSQL.WEEK_OFFSET) maps to tests that use that class even
    when base-class code is what reads the attribute.

Also replaces concurrent.futures.ProcessPoolExecutor with an in-process,
synchronous executor: sqlglot's optimizer and executor tests farm work out
to spawned processes, which coverage would not credit to the test. Arguments
and results are pickled round-trip, as a real process pool would do, so
serialization code (sqlglot/serde.py) is exercised and credited.
"""
import builtins
import concurrent.futures
import io
import json
import os
import pickle


class _SyncExecutor(concurrent.futures.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            result = fn(*args, **kwargs)
            future.set_result(pickle.loads(pickle.dumps(result)))
        except BaseException as exc:  # mirror a worker raising
            future.set_exception(exc)
        return future


concurrent.futures.ProcessPoolExecutor = _SyncExecutor

ROOT = os.path.realpath(os.environ.get("BR_ROOT", os.getcwd()))
OUT = os.environ["BR_OUT"]
_current = ["<collect>"]
_reads = {}
_uses = {}  # test -> set of classes
_orig_open = builtins.open


def _traced_open(file, *args, **kwargs):
    try:
        if isinstance(file, (str, bytes, os.PathLike)):
            path = os.path.realpath(os.fsdecode(file))
            if path.startswith(ROOT + os.sep) and not path.endswith((".py", ".pyc")):
                rel = os.path.relpath(path, ROOT)
                _reads.setdefault(_current[0], set()).add(rel)
    except Exception:
        pass
    return _orig_open(file, *args, **kwargs)


builtins.open = _traced_open
io.open = _traced_open


def _wrap_init(base):
    orig = base.__init__

    def __init__(self, *args, **kwargs):
        _uses.setdefault(_current[0], set()).add(type(self))
        orig(self, *args, **kwargs)

    base.__init__ = __init__


def _import_everything():
    """Import every sqlglot module before any test runs.

    sqlglot loads dialects (and their parser/generator/typing modules) lazily,
    so without this their import-time code (metaclasses, tables built by
    helper functions in class bodies) is credited to whichever test happened
    to trigger the import first in each worker. Importing up front puts all
    import-time execution in coverage's empty (non-test) context, which
    build_maps.py records as import_lines.
    """
    import importlib
    import pkgutil

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass


def pytest_collection(session):
    # Runs after coverage has started in this worker and before any test.
    _import_everything()

    from sqlglot import exp, generator, parser, tokens
    from sqlglot.dialects.dialect import Dialect

    for base in (Dialect, parser.Parser, generator.Generator, tokens.Tokenizer, exp.Expr):
        _wrap_init(base)
    # Expr subclasses that define their own __init__ (and may skip super()).
    seen = set()
    stack = [exp.Expr]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                stack.append(sub)
                if "__init__" in vars(sub):
                    _wrap_init(sub)

    orig_get = Dialect.__dict__["get_or_raise"].__func__

    def get_or_raise(cls, dialect):
        result = orig_get(cls, dialect)
        _uses.setdefault(_current[0], set()).add(type(result))
        return result

    Dialect.get_or_raise = classmethod(get_or_raise)


def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid


def pytest_runtest_logfinish(nodeid, location):
    _current[0] = "<between>"


def _class_key(cls):
    module = __import__("sys").modules.get(cls.__module__)
    path = getattr(module, "__file__", None)
    if not path:
        return None
    path = os.path.realpath(path)
    if not path.startswith(ROOT + os.sep):
        return None
    return f"{os.path.relpath(path, ROOT)}::{cls.__qualname__}"


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(OUT, exist_ok=True)
    with _orig_open(os.path.join(OUT, f"reads-{os.getpid()}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _reads.items()}, f)
    uses = {}
    for test, classes in _uses.items():
        keys = set()
        for cls in classes:
            for klass in cls.__mro__:
                key = _class_key(klass)
                if key:
                    keys.add(key)
        uses[test] = sorted(keys)
    with _orig_open(os.path.join(OUT, f"uses-{os.getpid()}.json"), "w") as f:
        json.dump(uses, f)
