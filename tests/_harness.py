"""Minimal check harness shared by the gates.

Deliberately not pytest: the gates must run anywhere the app runs, with no
dependency the app itself does not already have. Exit code is the contract -
0 means every invariant held, non-zero means do not ship.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Import the package from the repo, not from wherever the CWD happens to be.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Every gate touches render code, so Qt must be able to start headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class Gate:
    def __init__(self, name: str):
        self.name = name
        self.failures: list[str] = []
        self.passes = 0
        self._t0 = time.perf_counter()

    def section(self, title: str) -> None:
        print(f"\n  {title}")

    def check(self, ok: bool, msg: str) -> bool:
        if ok:
            self.passes += 1
            print(f"    ok    {msg}")
        else:
            self.failures.append(msg)
            print(f"    FAIL  {msg}")
        return bool(ok)

    def note(self, msg: str) -> None:
        print(f"          {msg}")

    def finish(self) -> int:
        dt = time.perf_counter() - self._t0
        print()
        if self.failures:
            print(f"  {self.name}: {len(self.failures)} FAILED "
                  f"of {self.passes + len(self.failures)} ({dt:.1f}s)")
            for f in self.failures:
                print(f"    - {f}")
            return 1
        print(f"  {self.name}: {self.passes} checks passed ({dt:.1f}s)")
        return 0
