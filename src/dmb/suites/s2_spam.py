"""S2 gate-spam: UCI SMS Spam Collection, binary gating.

Real data. Source: SMS Spam Collection v.1, Almeida & Hidalgo (2011),
distributed via UCI (https://archive.ics.uci.edu/dataset/228).
License: free for non-commercial research use with citation of Almeida,
T.A., Hidalgo, J.M.G., Yamakami, A. "Contributions to the Study of SMS
Spam Filtering" (CEAS 2011). The license does not permit relicensing or
commercial use, so DMB keeps the item text local: the published raw logs
redact S2 state text, and the report ships aggregate numbers only
(SPEC.md kill criterion).

300 items, stratified by class at the dataset prior; first 200 tagged
``eval``, last 100 ``spare``.
"""

from __future__ import annotations

import random
import zipfile
from pathlib import Path

import httpx

from .items import DMB_SEED, DecisionItem

SUITE_ID = "s2-gate-spam"
URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/00228/smsspamcollection.zip"
OPTIONS = ["ham", "spam"]
N_TOTAL = 300


def download(raw_dir: Path) -> Path:
    """Download and extract the collection; returns the data file path."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / "SMSSpamCollection"
    if not target.exists():
        zip_path = raw_dir / "smsspamcollection.zip"
        if not zip_path.exists():
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                response = client.get(URL)
                response.raise_for_status()
                zip_path.write_bytes(response.content)
        with zipfile.ZipFile(zip_path) as archive:
            member = archive.namelist()[0]
            target.write_bytes(archive.read(member))
    return target


def load_rows(path: Path) -> list[tuple[str, str]]:
    """Parse (label, text) rows; empty and duplicate texts dropped."""
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        label, text = line.split("\t", 1)
        text = text.strip()
        if label not in OPTIONS or len(text.split()) < 2 or text in seen:
            continue
        seen.add(text)
        rows.append((label, text))
    return rows


def build_items(rows: list[tuple[str, str]]) -> list[DecisionItem]:
    """Stratified sample at the dataset class prior, seeded."""
    rng = random.Random(f"{DMB_SEED}:s2")
    ham = [row for row in rows if row[0] == "ham"]
    spam = [row for row in rows if row[0] == "spam"]
    rng.shuffle(ham)
    rng.shuffle(spam)
    n_spam = round(N_TOTAL * len(spam) / len(rows))
    n_ham = N_TOTAL - n_spam
    sample = [(label, text) for label, text in spam[:n_spam]]
    sample += [(label, text) for label, text in ham[:n_ham]]
    rng.shuffle(sample)
    items: list[DecisionItem] = []
    for i, (label, text) in enumerate(sample):
        items.append(
            DecisionItem(
                item_id=f"s2-{i + 1:04d}",
                suite=SUITE_ID,
                state=text,
                options=list(OPTIONS),
                gold_index=OPTIONS.index(label),
                kind="gate",
                meta={
                    "label": label,
                    "split": "eval" if i < 200 else "spare",
                },
            )
        )
    return items
