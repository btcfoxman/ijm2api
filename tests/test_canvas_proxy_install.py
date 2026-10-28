"""Only verified upstream release bytes may enter the runtime image."""
import hashlib
import io
import os
from pathlib import Path
import tarfile
from unittest.mock import Mock

import pytest

from tools import install_canvas_proxy as installer


def elf(machine=62):
    binary = bytearray(64)
    binary[:6] = b"\x7fELF\x02\x01"
    binary[18:20] = machine.to_bytes(2, "little")
    return bytes(binary)


def archive(binary, *, link=False, extra=False, duplicate=False):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, content in (("./usr/bin/graftcp", binary),
                              ("./usr/share/doc/graftcp/COPYING", b"Synthetic GPL-3.0 license")):
            item = tarfile.TarInfo(name)
            if link and name.endswith("/graftcp"):
                item.type, item.linkname = tarfile.SYMTYPE, "../../outside"
                tar.addfile(item)
            else:
                item.size = len(content)
                tar.addfile(item, io.BytesIO(content))
                if duplicate and name.endswith("/graftcp"):
                    tar.addfile(item, io.BytesIO(content))
        if extra:
            item = tarfile.TarInfo("../../escaped-file")
            item.size = 4
            tar.addfile(item, io.BytesIO(b"evil"))
    return data.getvalue()


def configure(monkeypatch, contents, binary):
    monkeypatch.setattr(installer, "ARTIFACTS", {"linux-amd64": (
        "graftcp_v0.8.3_linux-amd64.tar.gz", hashlib.sha256(contents).hexdigest(),
        hashlib.sha256(binary).hexdigest())})
    request = Mock(side_effect=lambda *args, **kwargs: io.BytesIO(contents))
    monkeypatch.setattr(installer.urllib.request, "urlopen", request)
    return request


def test_installs_verified_amd64_binary_and_attribution_without_extracting_other_paths(tmp_path, monkeypatch):
    binary = elf()
    request = configure(monkeypatch, archive(binary, extra=True), binary)
    destination = tmp_path / "bin" / "graftcp"
    docs = tmp_path / "docs"
    installer.install(destination, license_directory=docs)
    assert destination.read_bytes() == binary
    assert (docs / "COPYING").read_bytes() == b"Synthetic GPL-3.0 license"
    assert "https://github.com/hmgle/graftcp/tree/v0.8.3" in (docs / "SOURCE").read_text()
    assert not (tmp_path / "escaped-file").exists()
    assert not (tmp_path.parent / "escaped-file").exists()
    assert request.call_args.args[0] == installer.BASE + installer.ARTIFACTS["linux-amd64"][0]
    if os.name != "nt":
        assert destination.stat().st_mode & 0o111


def test_archive_digest_mismatch_preserves_previous_binary_and_never_retries(tmp_path, monkeypatch):
    binary = elf()
    contents = archive(binary)
    request = configure(monkeypatch, contents, binary)
    monkeypatch.setattr(installer, "ARTIFACTS", {"linux-amd64": ("pinned.tar.gz", "0" * 64, hashlib.sha256(binary).hexdigest())})
    destination = tmp_path / "graftcp"
    destination.write_bytes(b"previous verified executable")
    with pytest.raises(ValueError, match="archive SHA256"):
        installer.install(destination)
    assert destination.read_bytes() == b"previous verified executable"
    assert not (tmp_path / "graftcp-license").exists()
    request.assert_called_once()


def test_binary_digest_mismatch_never_replaces_existing_destination(tmp_path, monkeypatch):
    binary = elf()
    configure(monkeypatch, archive(binary), binary + b"altered")
    destination = tmp_path / "graftcp"
    destination.write_bytes(b"previous")
    with pytest.raises(ValueError, match="executable SHA256"):
        installer.install(destination)
    assert destination.read_bytes() == b"previous"


@pytest.mark.parametrize("options", [{"link": True}, {"duplicate": True}])
def test_symlink_or_duplicate_executable_is_rejected(tmp_path, monkeypatch, options):
    binary = elf()
    configure(monkeypatch, archive(binary, **options), binary)
    with pytest.raises(ValueError, match="invalid usr/bin/graftcp"):
        installer.install(tmp_path / "graftcp")
    assert not (tmp_path / "graftcp").exists()


def test_wrong_elf_architecture_is_rejected_even_after_digest_checks(tmp_path, monkeypatch):
    binary = elf(machine=183)
    configure(monkeypatch, archive(binary), binary)
    with pytest.raises(ValueError, match="not Linux amd64 ELF"):
        installer.install(tmp_path / "graftcp")


def test_transient_download_failure_retries_without_installing_partial_bytes(tmp_path, monkeypatch):
    binary = elf()
    contents = archive(binary)
    request = configure(monkeypatch, contents, binary)
    request.side_effect = [OSError("synthetic transport failure"), io.BytesIO(contents)]
    sleep = Mock()
    monkeypatch.setattr(installer.time, "sleep", sleep)
    destination = tmp_path / "graftcp"
    installer.install(destination)
    assert destination.read_bytes() == binary
    assert request.call_count == 2
    sleep.assert_called_once_with(1)


def test_oversized_download_is_rejected_before_extraction(tmp_path, monkeypatch):
    binary = elf()
    configure(monkeypatch, archive(binary), binary)
    monkeypatch.setattr(installer, "MAX_ARCHIVE_BYTES", 4)
    with pytest.raises(ValueError, match="size limit"):
        installer.install(tmp_path / "graftcp")


def test_release_and_executable_digests_remain_pinned():
    assert installer.VERSION == "0.8.3"
    assert installer.BASE == "https://github.com/hmgle/graftcp/releases/download/v0.8.3/"
    assert installer.ARTIFACTS["linux-amd64"] == (
        "graftcp_v0.8.3_linux-amd64.tar.gz",
        "d5517e0e72e453dd2ef84a13cfbe409e7c7c3bbd45ab3ea53d298a7be5ddb5b4",
        "ba4da2c84d8c3660af565e174f931ed6592e1c76bf551f60b2cba2f664bc982d")
