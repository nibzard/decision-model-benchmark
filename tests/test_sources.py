"""Source checks validate cache bytes, licenses and exact archive members."""

import hashlib
import io
import zipfile

import httpx
import pytest

from dmb.suites import s1_intent77, s2_spam
from dmb.suites.sources import verified_download, verify_bytes


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def test_corrupted_cache_fails_without_network(tmp_path, monkeypatch):
    path = tmp_path / "input"
    path.write_bytes(b"wrong")
    monkeypatch.setattr(httpx.Client, "get", lambda *a: pytest.fail("must not redownload silently"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        verified_download(path, {"url": "https://invalid", "sha256": digest(b"expected")})
    assert path.read_bytes() == b"wrong"


def test_corrupted_download_is_not_cached(tmp_path, monkeypatch):
    path = tmp_path / "input"
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda *a: httpx.Response(
            200, content=b"changed", request=httpx.Request("GET", "https://invalid")
        ),
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        verified_download(path, {"url": "https://invalid", "sha256": digest(b"expected")})
    assert not path.exists()


def test_banking_cached_csv_still_fetches_and_checks_license(tmp_path, monkeypatch):
    payload = b"text,category\nexample,label\n"
    license_text = b"license fixture"
    (tmp_path / "banking77-train.csv").write_bytes(payload)
    monkeypatch.setitem(
        s1_intent77.SOURCES,
        "banking77_csv",
        {"url": "https://invalid/csv", "sha256": digest(payload)},
    )
    monkeypatch.setitem(
        s1_intent77.SOURCES,
        "banking77_license",
        {"url": "https://invalid/license", "sha256": digest(license_text)},
    )
    calls = []

    def get(client, url):
        calls.append(url)
        return httpx.Response(200, content=license_text, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    _, labels = s1_intent77.download(tmp_path)
    assert labels == ["label"]
    assert calls == ["https://invalid/license"]
    (tmp_path / "banking77-LICENSE").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum mismatch"):
        s1_intent77.download(tmp_path)


def test_sms_uses_named_member_and_validates_extracted_cache(tmp_path, monkeypatch):
    payload = b"ham\thello world\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("first-is-not-data", b"junk")
        archive.writestr("SMSSpamCollection", payload)
    (tmp_path / "smsspamcollection.zip").write_bytes(buffer.getvalue())
    monkeypatch.setitem(
        s2_spam.SOURCES,
        "sms_zip",
        {"url": "https://invalid/zip", "sha256": digest(buffer.getvalue())},
    )
    monkeypatch.setitem(
        s2_spam.SOURCES, "sms_data", {"member": "SMSSpamCollection", "sha256": digest(payload)}
    )
    target = s2_spam.download(tmp_path)
    assert target.read_bytes() == payload
    target.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum mismatch"):
        s2_spam.download(tmp_path)


def test_verify_bytes_returns_identical_payload():
    assert verify_bytes(b"fixture", digest(b"fixture"), "fixture") == b"fixture"


def test_default_items_directory_is_repository_data():
    from pathlib import Path

    from dmb.suites.items import items_dir

    assert items_dir() == Path(__file__).resolve().parents[1] / "data/suites"
