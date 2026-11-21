# Blast Radius selector

The entry selects tests for independent patches against SQLGlot at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. Build with the Dockerfile at
this directory's root. At runtime, it reads mounted patches and `tests.json`
from `/data`, and writes the requested JSON lines to stdout.

If a hunk's reported position disagrees with the source, the selector can
relocate the hunk when every old line matches at one location. Ambiguous
context triggers a conservative fallback. The selector applies every change
in a patch before running a verification probe. It maps edits to baseline
coverage, fixture and source-file reads, dictionary value reads, and recorded
operands of comparisons. For a supported comparison change, it evaluates
the new comparison against the recorded operands. Missing information can
broaden the selection. Small candidate groups can run against the patched
checkout. Only completed, passing tests may be removed. On Linux, a probe can fork a Python process that has loaded the standard library. The child then imports SQLGlot and its tests from the patched checkout.

The scheduler tries one through eight workers, scores each partition in
integer milliseconds, and emits the cheapest one it found. Empty selections
use no workers. The Docker build downloads the pinned SQLGlot archive and
precompiles its Python code. It also partitions the plain coverage map by source file, so each run loads only the portions it needs. Hash-based bytecode validation makes Python
recompile changed source in the temporary checkout.

## Regenerating the maps

The committed maps were generated from the unmodified pinned checkout. The
Docker build uses them as plain JSON files. To regenerate them offline,
use a Linux development machine with Python 3.13. Allow time for the full
instrumented suite, including the large parser stress test.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-build.txt
.venv/bin/python fetch_baseline.py ../sqlglot-baseline
SQLGLOT_ROOT="$PWD/../sqlglot-baseline" OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  .venv/bin/python build_map.py coverage-map.raw.json
.venv/bin/python tools/source_reads.py ../sqlglot-baseline coverage-map.raw.json "$PWD/source-reads.json"
.venv/bin/python tools/import_lines.py ../sqlglot-baseline coverage-map.raw.json "$PWD/import-lines.json"
.venv/bin/python tools/finalize_map.py coverage-map.raw.json ../sqlglot-baseline source-reads.json import-lines.json coverage-map.json
.venv/bin/python tools/build_config_coverage.py ../sqlglot-baseline coverage-map.json config-reads.json
```

The supplement instruments tokenizer configuration lookups. Each entry links
a baseline dictionary value to the tests that read it. The selector uses these
records for edits that retain the same dictionary keys. The collector checks
source hashes and traces value lookups across dialects.

`BR_PROBE_SECONDS=0` disables optional test execution. Successful prediction
on new patches depends on how well the recorded dependencies capture the
changed behavior, especially for dynamic imports, shared fixtures and
interactions between edits.
