"""S2 gate-spam: UCI SMS Spam Collection, binary gating.

Real data. Source: SMS Spam Collection v.1, Almeida & Hidalgo (2011),
distributed via UCI (https://archive.ics.uci.edu/dataset/228).
The current UCI dataset page lists CC BY 4.0. Cite Almeida, T.A., Hidalgo,
J.M.G., Yamakami, A., "Contributions to the Study of SMS Spam Filtering"
(CEAS 2011). DMB's publication policy keeps SMS text local: archives publish
approved numeric metadata only, including for shared result and probe logs.
This privacy policy does not assert a license prohibition on republication.

300 items, stratified by class at the dataset prior; first 200 tagged
``eval``, last 100 ``spare``.
"""

from __future__ import annotations

import random
import zipfile
from pathlib import Path

from .items import DMB_SEED, DecisionItem
from .sources import SOURCES, verified_download, verify_bytes

SUITE_ID = "s2-gate-spam"
URL = SOURCES["sms_zip"]["url"]
OPTIONS = ["ham", "spam"]
N_TOTAL = 300


def download(raw_dir: Path) -> Path:
    """Download and extract the collection; returns the data file path."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / "SMSSpamCollection"
    zip_path = verified_download(raw_dir / "smsspamcollection.zip", SOURCES["sms_zip"])
    if target.exists():
        verify_bytes(target.read_bytes(), SOURCES["sms_data"]["sha256"], target.name)
    else:
        with zipfile.ZipFile(zip_path) as archive:
            member = SOURCES["sms_data"]["member"]
            if archive.namelist().count(member) != 1:
                raise ValueError(f"expected exactly one ZIP member {member}")
            payload = verify_bytes(archive.read(member), SOURCES["sms_data"]["sha256"], member)
        target.write_bytes(payload)
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
        # Option order is shuffled per item so position bias cannot masquerade
        # as label skill on this skewed binary task.
        options = list(OPTIONS)
        rng.shuffle(options)
        items.append(
            DecisionItem(
                item_id=f"s2-{i + 1:04d}",
                suite=SUITE_ID,
                state=text,
                options=options,
                gold_index=options.index(label),
                kind="gate",
                meta={
                    "label": label,
                    "split": "eval" if i < 200 else "spare",
                },
            )
        )
    return items
