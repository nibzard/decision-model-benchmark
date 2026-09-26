"""Publication preserves measurements and restores only matching snapshots."""

import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

from dmb.prices import snapshot_hash

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/regenerate_corrections.py"
spec = importlib.util.spec_from_file_location("regenerate_corrections", MODULE_PATH)
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


def make_archive(path, row):
    payload = (json.dumps(row) + "\n").encode()
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("run/raw/provider.s2_spam.jsonl")
        member.size = len(payload)
        member.mtime = 12345
        archive.addfile(member, io.BytesIO(payload))


def test_observation_capture_ignores_payload_but_detects_measurement_changes(tmp_path):
    path = tmp_path / "raw.tar.gz"
    row = {
        "repeat": 1,
        "decision": {
            "choice_index": 0,
            "confidence": 0.8,
            "input_tokens": 10,
            "output_tokens": 1,
            "latency_ms": 4,
            "raw": {"reasoning_content": "private"},
        },
    }
    make_archive(path, row)
    original = publication.observations(path)
    row["decision"]["raw"] = {}
    make_archive(path, row)
    assert publication.observations(path) == original
    row["decision"]["confidence"] = 0.9
    make_archive(path, row)
    assert publication.observations(path) != original


def test_archive_normalization_is_reproducible(tmp_path):
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    make_archive(first, {"confidence": 0.8})
    make_archive(second, {"confidence": 0.8})
    publication.normalize_archive(first)
    publication.normalize_archive(second)
    assert first.read_bytes() == second.read_bytes()
    before = publication.observations(first)
    publication.normalize_archive(first)
    assert publication.observations(first) == before


def test_snapshot_restore_checks_exact_historical_hash(tmp_path):
    payload = [
        {
            "contender": "old",
            "model": "model",
            "input_per_mtok": 1,
            "output_per_mtok": 2,
            "checked_on": "2026-09-18",
            "source": "fixture",
        }
    ]
    (tmp_path / "manifest.json").write_text(
        json.dumps({"price_table_sha256": snapshot_hash(payload)})
    )
    publication.restore_snapshot(tmp_path, payload)
    assert json.loads((tmp_path / "prices.snapshot.json").read_text()) == payload
    payload[0]["input_per_mtok"] = 100
    (tmp_path / "prices.snapshot.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="historical price snapshot mismatch"):
        publication.restore_snapshot(tmp_path, payload)
