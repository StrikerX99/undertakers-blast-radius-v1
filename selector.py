"""Coverage-directed test selection for the pinned SQLGlot baseline.

The map contains baseline dependencies only. Patches are analyzed afresh; sample
answer labels are never read. Set BR_DATA, SQLGLOT_ROOT and BR_MAP for local use.
"""

from __future__ import annotations

import ast
import json
import marshal
from prepare_map import shard_name
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
import runtime_v2
import subprocess
import tempfile
from value_profile import compare, snapshot

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("BR_DATA", "/data"))
ROOT = Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", str(HERE / "coverage-map.json")))
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def structurally_equal(left, right):
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, ast.AST):
        return all(
            structurally_equal(getattr(left, name, None), getattr(right, name, None))
            for name in left._fields
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            structurally_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def split(tests: list[dict], worker_count: int) -> list[list[int]]:
    workers = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for test in sorted(tests, key=lambda t: (-round(t["seconds"] * 1000), t["number"])):
        i = min(range(worker_count), key=lambda j: (loads[j], j))
        workers[i].append(test["number"])
        loads[i] += round(test["seconds"] * 1000)
    return [worker for worker in workers if worker]


def schedule(tests: list[dict]) -> tuple[list[list[int]], int]:
    if not tests:
        return [], 0
    durations = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    best = None
    for count in range(1, min(8, len(tests)) + 1):
        workers = split(tests, count)
        # Local moves improve the longest-first schedule at negligible CPU cost.
        loads = [sum(durations[n] for n in w) for w in workers]
        for _ in range(16):
            high, low = (
                max(range(len(loads)), key=loads.__getitem__),
                min(range(len(loads)), key=loads.__getitem__),
            )
            gap = loads[high] - loads[low]
            movable = [n for n in workers[high] if 0 < durations[n] < gap]
            if not movable or high == low or len(workers[high]) <= 1:
                break
            n = min(movable, key=lambda n: abs(gap - 2 * durations[n]))
            workers[high].remove(n)
            workers[low].append(n)
            loads[high] -= durations[n]
            loads[low] += durations[n]
        cost = max(loads) + 2000 * len(workers)
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1], best[0]


def parse_patch(text: str) -> list[dict]:
    changes = []
    current = None
    hunk = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            current = {"old": None, "new": None, "hunks": []}
            changes.append(current)
            hunk = None
        elif line.startswith("--- ") and hunk is None:
            path = line[4:].strip().split("\t")[0]
            current["old"] = path[2:] if path.startswith("a/") else path
        elif line.startswith("+++ ") and hunk is None:
            path = line[4:].strip().split("\t")[0]
            current["new"] = path[2:] if path.startswith("b/") else path
        elif match := HUNK.match(line):
            hunk = {"start": int(match[1]), "count": int(match[2] or 1), "lines": []}
            current["hunks"].append(hunk)
        elif hunk is not None and line[:1] in (" ", "+", "-"):
            hunk["lines"].append(line)
        elif hunk is not None and line.startswith("\\ No newline"):
            hunk["lines"][-1] = hunk["lines"][-1].rstrip("\n")
    if not changes or any(c["old"] is None or c["new"] is None for c in changes):
        raise ValueError("Unsupported diff")
    return changes


def apply_change(original: str, change: dict) -> tuple[str, set[int], set[int]]:
    """Apply exactly matched hunks and return affected baseline coordinates.

    Prefer the advertised offset. If it is stale, every old-side line must
    match at one unique, nonoverlapping location. Ambiguous or missing text
    raises ValueError, allowing the caller to retain its conservative fallback.
    """
    source = original.splitlines(keepends=True)
    output = []
    removed = set()
    inserted = set()
    cursor = 0
    for hunk in change["hunks"]:
        lines = hunk["lines"]
        old_lines = [line[1:] for line in lines if line[0] in " -"]
        if len(old_lines) != hunk["count"]:
            raise ValueError("Hunk old-side length disagrees with its header")
        start = hunk["start"] - 1 if old_lines else hunk["start"]
        if (
            start < cursor
            or start > len(source)
            or source[start : start + len(old_lines)] != old_lines
        ):
            if not old_lines:
                raise ValueError("Cannot relocate an unanchored insertion")
            matches = (
                i
                for i in range(cursor, len(source) - len(old_lines) + 1)
                if source[i] == old_lines[0]
                and source[i : i + len(old_lines)] == old_lines
            )
            start = next(matches, None)
            if start is None or next(matches, None) is not None:
                raise ValueError("Hunk does not uniquely match the pinned source")
        output.extend(source[cursor:start])
        cursor = start
        for line in lines:
            if line[0] == "+":
                inserted.add(max(1, cursor))
                output.append(line[1:])
            else:
                cursor += 1
                if line[0] == "-":
                    removed.add(cursor)
                else:
                    output.append(line[1:])
    output.extend(source[cursor:])
    return "".join(output), removed, inserted


class MappedSets:
    def __init__(self, mapping, by_name):
        self.indices = mapping["sets"]
        self.numbers = [by_name.get(name) for name in mapping["tests"]]
        self.cache = {}

    def __getitem__(self, key):
        if key not in self.cache:
            numbers = self.numbers
            self.cache[key] = {
                numbers[index]
                for index in self.indices[key]
                if numbers[index] is not None
            }
        return self.cache[key]


class Selector:
    def __init__(self, tests, mapping):
        if mapping.get(
            "commit"
        ) != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or mapping.get(
            "baseline_failures"
        ):
            raise ValueError("Invalid baseline map")
        self.tests = tests
        self.all = {t["number"] for t in tests}
        self.by_number = {t["number"]: t for t in tests}
        self.by_name = {t["id"]: t["number"] for t in tests}
        known_names = set(mapping["tests"])
        self.unknown = {t["number"] for t in tests if t["id"] not in known_names}
        # Only a small fraction of the map is used in any batch. Translate
        # test identifiers on demand rather than duplicating its entire index.
        self.sets = MappedSets(mapping, self.by_name)
        self.lines = mapping["lines"]
        self.files = mapping["files"]
        self.source_cache = {}
        self.file_cache = {}
        self.mapping = mapping
        self.shards = mapping.get("paths", set())
        self.shard_directory = HERE / "map-index"

    def load_shard(self, path):
        if path not in self.shards:
            return
        blob = marshal.loads((self.shard_directory / shard_name(path)).read_bytes())
        self.sets.indices.update(blob.pop("sets"))
        for field, rows in blob.items():
            table = self.mapping.setdefault(field, {})
            if field == "regions":
                old = table.get(path, {})
                table[path] = {**rows, **old}
            else:
                table[path] = rows
        self.shards.remove(path)

    def source(self, path):
        self.load_shard(path)
        if path not in self.source_cache:
            p = PurePosixPath(path)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("Unsafe patch path")
            raw = (ROOT / path).read_bytes()
            expected = self.mapping.get("source_hashes", {}).get(path)
            if expected and hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("Source does not match dependency map")
            text = raw.decode("utf-8")
            tree = ast.parse(text) if path.endswith(".py") else None
            self.source_cache[path] = (text, tree)
        return self.source_cache[path]

    def line_tests(self, path, lines):
        rows = self.lines.get(path, {})
        result = set()
        for line in lines:
            key = rows.get(str(line))
            if key is not None:
                result.update(self.sets[key])
        return result

    def file_tests(self, path):
        if path not in self.file_cache:
            result = set()
            for key in self.lines.get(path, {}).values():
                result.update(self.sets[key])
            if path in self.files:
                result.update(self.sets[self.files[path]])
            result.update(t["number"] for t in self.tests if t["file"] == path)
            self.file_cache[path] = result
        return self.file_cache[path]

    def profiled_changes(self, path, old, new, in_function=False):
        """Refine coverage using values observed on the unchanged baseline.

        Only replay side-effect-free comparisons with unchanged operands (or
        literal operands). Unknown values keep their tests. Other edits use the
        ordinary dependency map; there is no patch or edit-kind answer table.
        """
        if structurally_equal(old, new):
            return set()
        if (
            isinstance(old, ast.Compare)
            and isinstance(new, ast.Compare)
            and len(old.ops) == len(new.ops) == 1
            and in_function
        ):
            key = f"{old.lineno}:{old.col_offset}:{old.end_lineno}:{old.end_col_offset}"
            record = self.mapping.get("comparisons", {}).get(path, {}).get(key)
            if record is None:
                raise ValueError("No operand observations")
            replacements = []
            for a, b in [
                (old.left, new.left),
                (old.comparators[0], new.comparators[0]),
            ]:
                if ast.dump(a) == ast.dump(b):
                    replacements.append(None)
                else:
                    try:
                        ast.literal_eval(a)
                        value = snapshot(ast.literal_eval(b))
                    except (ValueError, TypeError, SyntaxError):
                        raise ValueError("Operand expression changed")
                    if value[0] == "unknown":
                        raise ValueError("Unsupported operand")
                    replacements.append(value)
            affected = set(self.sets[record["unknown"]])
            for values, index in record["variants"]:
                left, right, identical, before = values
                if replacements[0] is not None:
                    left = replacements[0]
                if replacements[1] is not None:
                    right = replacements[1]
                op = type(new.ops[0]).__name__
                if any(v is not None for v in replacements) and op in ("Is", "IsNot"):
                    affected.update(self.sets[index])
                    continue
                try:
                    after = compare(op, left, right, identical)
                except (ValueError, TypeError, KeyError):
                    affected.update(self.sets[index])
                    continue
                if before != after:
                    affected.update(self.sets[index])
            return affected
        if isinstance(old, ast.Dict) and isinstance(new, ast.Dict) and not in_function:
            if len(old.keys) != len(new.keys) or any(
                a is None or b is None or ast.dump(a) != ast.dump(b)
                for a, b in zip(old.keys, new.keys)
            ):
                raise ValueError("Dictionary shape changed")
            affected = set()
            for key, a, b in zip(old.keys, old.values, new.values):
                if ast.dump(a) == ast.dump(b):
                    continue
                # Literal data edits have no execution side effects of their own.
                ast.literal_eval(a)
                ast.literal_eval(b)
                region = (
                    f"{key.lineno}:{key.col_offset}:{a.end_lineno}:{a.end_col_offset}"
                )
                index = self.mapping.get("regions", {}).get(path, {}).get(region)
                if index is None:
                    raise ValueError("No entry dependency observations")
                affected.update(self.sets[index])
            return affected
        if type(old) is not type(new):
            raise ValueError("Different expression structure")
        affected = set()
        in_function = in_function or isinstance(
            old, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        )
        for field in old._fields:
            a, b = getattr(old, field, None), getattr(new, field, None)
            if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                affected.update(self.profiled_changes(path, a, b, in_function))
            elif isinstance(a, list) and isinstance(b, list):
                if len(a) != len(b):
                    raise ValueError("Statement count changed")
                for x, y in zip(a, b):
                    if isinstance(x, ast.AST) and isinstance(y, ast.AST):
                        affected.update(self.profiled_changes(path, x, y, in_function))
                    elif x != y:
                        raise ValueError("Unsupported edit")
            elif a != b:
                raise ValueError("Unsupported edit")
        return affected

    def select(self, patch) -> set[int]:
        chosen = set(self.unknown)
        self.import_modules = set()
        for change in parse_patch(patch) if isinstance(patch, str) else patch:
            path = change["old"]
            if path == "/dev/null" or change["new"] != path:
                return set(self.all)
            original, tree = self.source(path)
            updated, removed, inserted = apply_change(original, change)
            if original == updated:
                continue
            if tree is None:
                # Non-Python files are data dependencies, including fixture comments.
                chosen.update(self.file_tests(path) or self.all)
                continue
            try:
                new_tree = ast.parse(updated)
                compile(updated.encode("utf-8"), path, "exec")
            except (SyntaxError, ValueError, TypeError):
                # A failed import can prevent an entire test module from collecting.
                if path.startswith("tests/test_") or path.startswith(
                    "tests/dialects/test_"
                ):
                    chosen.update(t["number"] for t in self.tests if t["file"] == path)
                else:
                    chosen.update(self.all)
                continue
            if structurally_equal(tree, new_tree):
                # Ignore actual Python comments/formatting, not comment-looking strings.
                if path in self.files:
                    chosen.update(self.sets[self.files[path]])
                continue
            imported = set(self.mapping.get("import_lines", {}).get(path, []))
            if path.startswith("sqlglot/") and (removed | inserted) & imported:
                module = path[:-3].replace("/", ".")
                if module.endswith(".__init__"):
                    module = module[:-9]
                self.import_modules.add(module)
            try:
                refined = self.profiled_changes(path, tree, new_tree)
            except (ValueError, TypeError, SyntaxError, RecursionError):
                refined = None
            if refined is not None:
                chosen.update(refined)
                if path in self.files:
                    chosen.update(self.sets[self.files[path]])
                continue
            changed_lines = removed | inserted
            candidates = self.line_tests(path, changed_lines)
            # Multiline expressions may only execute their first physical line.
            # Expand to the innermost statement, but keep individual dictionary
            # entries precise when their own data dependency was observed.
            for line in changed_lines:
                nodes = [
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, ast.stmt)
                    and n.lineno <= line <= getattr(n, "end_lineno", n.lineno)
                ]
                if not nodes:
                    candidates.update(self.file_tests(path) or self.all)
                    continue
                stmt = min(nodes, key=lambda n: n.end_lineno - n.lineno)
                if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.ClassDef)):
                    candidates.update(self.all)
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    candidates.update(
                        self.line_tests(path, range(stmt.lineno, stmt.end_lineno + 1))
                    )
                elif isinstance(stmt, (ast.If, ast.While, ast.Assert)):
                    candidates.update(
                        self.line_tests(
                            path, range(stmt.test.lineno, stmt.test.end_lineno + 1)
                        )
                    )
                elif not self.line_tests(path, [line]):
                    candidates.update(
                        self.line_tests(path, range(stmt.lineno, stmt.end_lineno + 1))
                    )
                # Insertions can introduce an unconditional effect in a previously
                # untaken branch: include the enclosing executable block.
                if line in inserted and not removed:
                    functions = [
                        n
                        for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.lineno <= line <= n.end_lineno
                    ]
                    if functions:
                        fn = min(functions, key=lambda n: n.end_lineno - n.lineno)
                        candidates.update(
                            self.line_tests(path, range(fn.lineno, fn.end_lineno + 1))
                        )
                    else:
                        candidates.update(self.file_tests(path) or self.all)
            if not candidates:
                functions = [
                    n
                    for n in ast.walk(tree)
                    if isinstance(
                        n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                    )
                ]
                inside = all(
                    any(n.lineno < line <= n.end_lineno for n in functions)
                    for line in changed_lines
                )
                imported = set(self.mapping.get("import_lines", {}).get(path, []))
                if changed_lines & imported:
                    # For literal class/module data, broaden to every observed
                    # user of the module. The separate import check catches
                    # initialization failures. Executable import-time edits
                    # retain the full suite.
                    literal_data = True
                    for line in changed_lines:
                        statements = [
                            n
                            for n in ast.walk(tree)
                            if isinstance(n, (ast.Assign, ast.AnnAssign))
                            and n.lineno <= line <= n.end_lineno
                        ]
                        if not statements:
                            literal_data = False
                            break
                        try:
                            ast.literal_eval(
                                min(
                                    statements, key=lambda n: n.end_lineno - n.lineno
                                ).value
                            )
                        except (ValueError, TypeError):
                            literal_data = False
                            break
                    candidates = (
                        (self.file_tests(path) or set(self.all))
                        if literal_data
                        else set(self.all)
                    )
                elif not inside or not removed:
                    candidates = self.file_tests(path) or set(self.all)
            if path in self.files:
                candidates.update(self.sets[self.files[path]])
            chosen.update(candidates)
        return chosen


def patched_checkout(destination, changes, selector):
    """Share baseline files read-only; materialize only patched paths in /tmp."""
    destination.mkdir()
    for child in ROOT.iterdir():
        if child.name != ".git":
            (destination / child.name).symlink_to(
                child, target_is_directory=child.is_dir()
            )
    for change in changes:
        path = change["old"]
        if path == "/dev/null" or change["new"] != path:
            raise ValueError("Unsupported overlay change")
        original, _ = selector.source(path)
        updated, _, _ = apply_change(original, change)
        relative = PurePosixPath(path)
        parent = destination
        for part in relative.parts[:-1]:
            parent /= part
            if parent.is_symlink():
                target = parent.resolve()
                parent.unlink()
                parent.mkdir()
                for child in target.iterdir():
                    (parent / child.name).symlink_to(
                        child, target_is_directory=child.is_dir()
                    )
        output = destination / relative
        if output.is_symlink():
            output.unlink()
        output.write_text(updated)


def verify_imports(selector, changes):
    if not selector.import_modules:
        return True
    with tempfile.TemporaryDirectory(prefix="br-import-") as temporary:
        root = Path(temporary) / "repo"
        patched_checkout(root, changes, selector)
        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
        code = "import importlib,sys; [importlib.import_module(name) for name in sys.argv[1:]]"
        try:
            result = subprocess.run(
                [sys.executable, "-c", code, *sorted(selector.import_modules)],
                cwd=root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False


def attach_value_reads(mapping):
    path = HERE / "config-reads.json"
    if not path.exists():
        return
    records = json.loads(path.read_bytes())
    if records["commit"] != mapping["commit"]:
        return
    names = {name: i for i, name in enumerate(mapping["tests"])}
    for source, sites in records["value_reads"].items():
        rows = mapping["regions"].setdefault(source, {})
        for span, readers in sites.items():
            # Distinct from general line coverage: these are baseline reads
            # of an entry's value, for changes that preserve its key.
            if isinstance(mapping["sets"], dict):
                slot = -1 - sum(1 for key in mapping["sets"] if key < 0)
                mapping["sets"][slot] = [names[reader] for reader in readers]
            else:
                slot = len(mapping["sets"])
                mapping["sets"].append([names[reader] for reader in readers])
            rows[span] = slot


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    try:
        index = HERE / "map-index" / "index"
        if "BR_MAP" not in os.environ and index.is_file():
            mapping = marshal.loads(index.read_bytes())
            for field in ("lines", "regions", "comparisons", "import_lines"):
                mapping[field] = {}
        else:
            mapping = json.loads(MAP.read_bytes())
        attach_value_reads(mapping)
        selector = Selector(tests, mapping)
    except Exception as exc:
        print(f"Map unavailable; conservative fallback: {exc}", file=sys.stderr)
        selector = None
    probe_seconds = float(os.environ.get("BR_PROBE_SECONDS", "1.0"))
    # Leave headroom in the shortest published three-minute batch budget.
    deadline = time.monotonic() + 135
    for patch in sorted((DATA / "commits").glob("*.patch")):
        selected = {t["number"] for t in tests}
        if selector and time.monotonic() < deadline:
            try:
                changes = parse_patch(patch.read_text())
                candidates = selector.select(changes)
                probing = probe_seconds > 0 and runtime_v2.eligible(
                    selector, changes, candidates
                )
                if probing:
                    try:
                        selected = runtime_v2.verify(
                            selector,
                            changes,
                            candidates,
                            probe_seconds,
                            patched_checkout,
                            remaining_wall=deadline - time.monotonic(),
                        )
                    except Exception as exc:
                        print(
                            f"{patch.stem}: verification fallback: {exc}",
                            file=sys.stderr,
                        )
                        selected = (
                            set(selector.all) if selector.import_modules else candidates
                        )
                elif verify_imports(selector, changes):
                    selected = candidates
                else:
                    print(
                        f"{patch.stem}: import failed; retaining all tests",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(f"{patch.stem}: conservative fallback: {exc}", file=sys.stderr)
        workers, cost = schedule([t for t in tests if t["number"] in selected])
        print(
            json.dumps(
                {"commit": patch.stem, "workers": workers}, separators=(",", ":")
            )
        )
        print(
            f"{patch.stem}: tests={len(selected)}, workers={len(workers)}, simulated={cost / 1000:.3f}s",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
