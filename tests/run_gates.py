"""Run every gate. Exit 0 = safe to ship, non-zero = do not.

    py tests/run_gates.py           everything
    py tests/run_gates.py truth     just the data-truth gate (fast, ~1 s)

The truth gate is cheap enough to run on every commit; the performance gate
takes about a minute because it has to build hours of feed to measure growth.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATES = {
    "truth": "gate_truth.py",
    "perf": "gate_perf.py",
    "frames": "gate_frames.py",
    "gc": "gc_gate.py",
}


def main(argv: list[str]) -> int:
    want = argv[1:] or list(GATES)
    unknown = [w for w in want if w not in GATES]
    if unknown:
        print(f"unknown gate(s): {', '.join(unknown)}")
        print(f"available: {', '.join(GATES)}")
        return 2

    results = {}
    for name in want:
        print(f"\n{'=' * 68}\n{name.upper()} GATE\n{'=' * 68}")
        rc = subprocess.run([sys.executable, str(HERE / GATES[name])],
                            cwd=HERE).returncode
        results[name] = rc

    print(f"\n{'=' * 68}")
    for name, rc in results.items():
        print(f"  {'PASS' if rc == 0 else 'FAIL'}  {name}")
    bad = [n for n, rc in results.items() if rc != 0]
    if bad:
        print(f"\n{len(bad)} gate(s) failed: {', '.join(bad)} - DO NOT SHIP")
        return 1
    print("\nall gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
