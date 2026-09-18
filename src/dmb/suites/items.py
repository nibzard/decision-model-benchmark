"""Frozen decision items and the JSONL format used on disk.

A suite is a JSONL file under ``data/suites/<suite_id>.jsonl``. Items are
frozen at release: the suite builder writes the file once, and the file's
SHA-256 goes into every run manifest. ``gold_index`` is -1 when no correct
option exists (suite S5 no-good-option items).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, Field

# One seed for every generator in the benchmark. Frozen.
DMB_SEED = 20260918


class DecisionItem(BaseModel):
    """One typed decision: a state, N options, and a gold option index."""

    item_id: str
    suite: str
    state: str
    options: list[str]
    gold_index: int
    kind: str | None = None
    meta: dict[str, object] = Field(default_factory=dict)
    # S4 provenance: which base item and which permutation of the option order.
    base_item_id: str | None = None
    permutation: int | None = None


def save_items(items: list[DecisionItem], path: Path) -> str:
    """Write items as JSONL and return the file's SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [it.model_dump_json() for it in items]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sha256_file(path)


def load_items(path: Path) -> list[DecisionItem]:
    """Load items from a JSONL file."""
    return [
        DecisionItem.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, streaming in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def items_dir(root: Path | None = None) -> Path:
    """Directory holding the frozen suite files: ``data/suites``."""
    base = root if root is not None else Path(__file__).resolve().parents[2]
    return base / "data" / "suites"
