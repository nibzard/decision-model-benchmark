"""S1 intent-77: Banking77 intent classification, 77-way routing.

Real data. Source: the Banking77 dataset, PolyAI-LD task-specific
datasets (https://github.com/PolyAI-LD/task-specific-datasets,
banking_data/train.csv). License: Creative Commons Attribution 4.0
International (CC BY 4.0) - the repository's LICENSE file is saved to
``data/raw/banking77-LICENSE`` at build time and its hash recorded.
CC BY 4.0 permits redistribution with attribution, so S1 item text may
appear in published raw logs with this notice.

300 items, stratified round-robin over the 77 intent labels; first 200
tagged ``eval``, last 100 ``spare`` (the spare items feed S4). The 77
option labels are serialized once, in alphabetical order, and are
identical for every item.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

import httpx

from .items import DMB_SEED, DecisionItem

SUITE_ID = "s1-intent-77"
DATA_URL = (
    "https://raw.githubusercontent.com/PolyAI-LD/task-specific-datasets/"
    "master/banking_data/train.csv"
)
LICENSE_URL = (
    "https://raw.githubusercontent.com/PolyAI-LD/task-specific-datasets/"
    "master/LICENSE"
)
N_TOTAL = 300


def download(raw_dir: Path) -> tuple[Path, list[str]]:
    """Download train.csv plus its LICENSE; returns (csv path, labels)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    csv_path = raw_dir / "banking77-train.csv"
    if not csv_path.exists():
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            response = client.get(DATA_URL)
            response.raise_for_status()
            csv_path.write_bytes(response.content)
            try:
                license_response = client.get(LICENSE_URL)
                if license_response.status_code == 200:
                    (raw_dir / "banking77-LICENSE").write_bytes(license_response.content)
            except httpx.HTTPError:
                pass  # license text stays absent; recorded in data/LICENSES.md
    labels: list[str] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            labels.append(row["label"])
    return csv_path, sorted(set(labels))


def load_rows(path: Path) -> list[tuple[str, str]]:
    """Parse (text, label) rows; duplicate texts dropped."""
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            text = row["text"].strip()
            if not text or text in seen:
                continue
            seen.add(text)
            rows.append((text, row["label"]))
    return rows


def build_items(rows: list[tuple[str, str]], labels: list[str]) -> list[DecisionItem]:
    """Stratified round-robin sample over labels, seeded."""
    if len(labels) != 77:
        raise ValueError(f"expected 77 banking77 labels, found {len(labels)}")
    rng = random.Random(f"{DMB_SEED}:s1")
    by_label: dict[str, list[str]] = {label: [] for label in labels}
    for text, label in rows:
        by_label[label].append(text)
    for label in labels:
        rng.shuffle(by_label[label])
    order = list(labels)
    rng.shuffle(order)
    sample: list[str] = []  # flattened texts, round-robin over shuffled labels
    round_index = 0
    while len(sample) < N_TOTAL:
        progressed = False
        for label in order:
            bucket = by_label[label]
            if round_index < len(bucket):
                sample.append(bucket[round_index])
                progressed = True
                if len(sample) >= N_TOTAL:
                    break
        if not progressed:
            raise ValueError("not enough unique texts for 300 items")
        round_index += 1
    text_to_label = {t: lbl for t, lbl in rows}
    items = []
    for i, text in enumerate(sample):
        label = text_to_label[text]
        items.append(
            DecisionItem(
                item_id=f"s1-{i + 1:04d}",
                suite=SUITE_ID,
                state=text,
                options=list(labels),
                gold_index=labels.index(label),
                kind="intent",
                meta={
                    "label": label,
                    "split": "eval" if i < 200 else "spare",
                },
            )
        )
    return items


def options_block(labels: list[str]) -> str:
    """The 77 options serialized once (canonical block, for the record)."""
    return "\n".join(f"[{i}] {label}" for i, label in enumerate(labels))
