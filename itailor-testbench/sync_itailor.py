# iTailor testbench - Copyright (c) 2026 Michael Bidollahkhani. All rights reserved. MIT License.
"""Copy the library package from ../itailor/itailor into ./itailor so the bench tests exactly that code.

    python sync_itailor.py            # copy
    python sync_itailor.py --check    # only report whether the copy is up to date
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.environ.get("ITAILOR_SOURCE") or os.path.join(os.path.dirname(HERE), "itailor", "itailor")
TARGET = os.path.join(HERE, "itailor")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def differences(a: str, b: str) -> list[str]:
    out: list[str] = []
    if not os.path.isdir(b):
        return ["(no copy yet)"]
    cmp = filecmp.dircmp(a, b, ignore=["__pycache__", "SYNCED_FROM.txt"])

    def walk(node, prefix=""):
        out.extend(prefix + n for n in node.left_only)
        out.extend(prefix + n + " (only in copy)" for n in node.right_only)
        out.extend(prefix + n + " (differs)" for n in node.diff_files)
        for name, sub in node.subdirs.items():
            walk(sub, prefix + name + "/")
    walk(cmp)
    return out


def main() -> int:
    if not os.path.isdir(SOURCE):
        print(f"source not found: {SOURCE} (set ITAILOR_SOURCE)")
        return 1
    if "--check" in sys.argv:
        diff = differences(SOURCE, TARGET)
        print("up to date" if not diff else "out of date:\n  " + "\n  ".join(diff))
        return 0 if not diff else 2
    if os.path.isdir(TARGET):
        shutil.rmtree(TARGET)
    shutil.copytree(SOURCE, TARGET, ignore=IGNORE)
    with open(os.path.join(TARGET, "SYNCED_FROM.txt"), "w", encoding="utf-8") as handle:
        handle.write(f"copied from {SOURCE}\n")
    print(f"copied {SOURCE} -> {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
