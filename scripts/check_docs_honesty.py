#!/usr/bin/env python3
"""Docs-honesty lint — CI gate G-DOCS (DELIVERY-PLAN §6.5, NFR-5).

The brand is correctness *and honesty about its limits*. This lint keeps that true
as contributors arrive, by failing the build when the docs overclaim.

Two rules:

1. **Never claim "exactly-once delivery" un-negated.** Exactly-once *delivery* is
   impossible (Two Generals / FLP). Any sentence containing "exactly-once delivery"
   must also negate it ("not", "impossible", "cannot", "isn't", "no such thing",
   "a lie", "myth"). This is the one sentence that would sink the brand.

2. **The scoping language must exist.** Each scanned doc that describes the
   guarantee must, somewhere, scope "exactly-once" as an *effect* — one of the
   qualifiers ("effect", "at most once", "at-most-once", "not delivery",
   "replay-on-success") must appear. Silence about the boundary is overclaim by
   omission.

Run: ``python scripts/check_docs_honesty.py``  (exit 1 on any violation).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files whose prose makes guarantees to readers.
TARGETS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

_NEGATORS = ("not", "impossible", "cannot", "can't", "isn't", "is not", "no such thing",
             "a lie", "myth", "never", "without")
_SCOPES = ("effect", "at most once", "at-most-once", "not delivery", "replay-on-success",
           "replay on success")
_DELIVERY = re.compile(r"exactly[- ]once\s+delivery", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\n])\s+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT)

    # Rule 1: no un-negated "exactly-once delivery".
    for sentence in _sentences(text):
        if _DELIVERY.search(sentence):
            low = sentence.lower()
            if not any(neg in low for neg in _NEGATORS):
                problems.append(
                    f"{rel}: claims 'exactly-once delivery' without negating it — "
                    f"delivery is impossible; scope it as effect/at-most-once:\n    “{sentence[:160]}”"
                )

    # Rule 2: the scoping language must be present somewhere in the doc.
    low_text = text.lower()
    if "exactly-once" in low_text and not any(scope in low_text for scope in _SCOPES):
        problems.append(
            f"{rel}: mentions 'exactly-once' but never scopes it (no 'effect' / "
            "'at most once' / 'not delivery' qualifier anywhere in the file)."
        )
    return problems


def main() -> int:
    all_problems: list[str] = []
    for path in TARGETS:
        if path.exists():
            all_problems.extend(check_file(path))

    if all_problems:
        print("✗ docs-honesty lint FAILED:\n")
        for p in all_problems:
            print(f"  • {p}\n")
        return 1
    print(f"✓ docs-honesty lint passed ({len([t for t in TARGETS if t.exists()])} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
