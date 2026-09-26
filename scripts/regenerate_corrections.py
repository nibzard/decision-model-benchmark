"""Reproduce v3 and sanitize every public archive, without model-provider calls.

Run after `uv run dmb build`. Archive extraction is restricted to data files.
Historical price snapshots are restored only when their manifest hash matches
an exact checked-in historical payload; current prices are never substituted.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from dmb.prices import snapshot_hash
from dmb.report import archive_runs, render_report
from dmb.suites.build import build_all

ROOT = Path(__file__).resolve().parents[1]
ARCHIVES = (
    "results/v1-raw.tar.gz",
    "results/v1.1-raw.tar.gz",
    "results/v2/v2-raw.tar.gz",
)
OBSERVATION_FIELDS = (
    "choice_index",
    "confidence",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "ok",
    "malformed",
    "retries",
    "repeat",
    "attempt_index",
    "attempt",
    "gold_index",
)


def observations(archive_path: Path) -> dict:
    """Capture primary numerical measurements per log; exclude arbitrary text."""
    captured = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith(".jsonl"):
                continue
            stream = archive.extractfile(member)
            rows = []
            for line in stream.read().decode().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                measured = {key: row[key] for key in OBSERVATION_FIELDS if key in row}
                decision = row.get("decision")
                if isinstance(decision, dict):
                    measured["decision"] = {
                        key: decision[key] for key in OBSERVATION_FIELDS if key in decision
                    }
                rows.append(measured)
            payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
            captured[member.name] = {
                "rows": len(rows),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
    return captured


def normalize_archive(path: Path) -> None:
    """Fix tar/gzip metadata so identical content has reproducible archive bytes."""
    buffer = io.BytesIO()
    with tarfile.open(path, "r:gz") as original, tarfile.open(fileobj=buffer, mode="w") as output:
        for member in original:
            if not member.isfile():
                continue
            member.mtime = member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mode = 0o644
            member.pax_headers = {}
            output.addfile(member, original.extractfile(member))
    path.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))


def restore_snapshot(run_dir: Path, historical_payload: list[dict]) -> None:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    snapshot = run_dir / "prices.snapshot.json"
    if snapshot.exists():
        payload = json.loads(snapshot.read_text())
    else:
        payload = historical_payload
    if snapshot_hash(payload) != manifest["price_table_sha256"]:
        raise ValueError(f"historical price snapshot mismatch for {run_dir.name}")
    snapshot.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")


def regenerate(root: Path = ROOT) -> dict:
    build_all(root)
    historical_payload = json.loads((root / "scripts/fixtures/prices-2026-09-18.json").read_text())
    before = {name: observations(root / name) for name in ARCHIVES}
    with tempfile.TemporaryDirectory(prefix="dmb-corrections-") as temporary:
        staging = Path(temporary)
        (staging / "data").symlink_to(root / "data", target_is_directory=True)
        consolidated = staging / "runs"
        consolidated.mkdir()
        archive_runs_by_name = {}
        for index, name in enumerate(ARCHIVES):
            extracted = staging / f"archive-{index}"
            extracted.mkdir()
            with tarfile.open(root / name, "r:gz") as archive:
                archive.extractall(extracted, filter="data")
            run_dirs = sorted(
                path
                for path in extracted.iterdir()
                if path.is_dir() and (path / "manifest.json").exists()
            )
            if not run_dirs:
                raise ValueError(f"archive has no run manifests: {name}")
            archive_runs_by_name[name] = run_dirs
            for run_dir in run_dirs:
                restore_snapshot(run_dir, historical_payload)
                destination = consolidated / run_dir.name
                if not destination.exists():
                    shutil.copytree(run_dir, destination)
                else:
                    # Duplicate releases must contain the same measured observations.
                    if (destination / "results.jsonl").read_bytes() != (
                        run_dir / "results.jsonl"
                    ).read_bytes():
                        raise ValueError(f"conflicting archived run {run_dir.name}")
        expected = [consolidated / name for name in ("v1", "v1.1", "v2-majority-fix")]
        if any(not path.exists() for path in expected):
            raise ValueError("missing historical source run")
        render_report(
            expected[0],
            root / "results/v3",
            extra_run_dirs=expected[1:],
            name="v3",
            allow_protocol_mix=True,
            correction_note_path=root / "results/v3/CORRECTIONS.md",
        )
        normalize_archive(root / "results/v3/v3-raw.tar.gz")
        for name, run_dirs in archive_runs_by_name.items():
            replacement = staging / (Path(name).name + ".sanitized")
            archive_runs(run_dirs, replacement)
            normalize_archive(replacement)
            if observations(replacement) != before[name]:
                raise ValueError(f"sanitization changed numeric observations: {name}")
            shutil.copyfile(replacement, root / name)
    proof = {
        "source_archives": before,
        "preserved": True,
        "source_runs": ["v1", "v1.1", "v2-majority-fix"],
        "source_price_snapshot_sha256": snapshot_hash(historical_payload),
    }
    (root / "results/v3/observations-preserved.json").write_text(
        json.dumps(proof, sort_keys=True, indent=2) + "\n"
    )
    print(
        "Regenerated v3; sanitized all three historical archives; numeric observations unchanged."
    )
    return proof


if __name__ == "__main__":
    regenerate()
