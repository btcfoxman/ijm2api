"""Install the immutable upstream graftcp binary during a CI image build."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import tarfile
import tempfile
import time
import urllib.request


VERSION = "0.8.3"
BASE = f"https://github.com/hmgle/graftcp/releases/download/v{VERSION}/"
ARTIFACTS = {
    "linux-amd64": (
        "graftcp_v0.8.3_linux-amd64.tar.gz",
        "d5517e0e72e453dd2ef84a13cfbe409e7c7c3bbd45ab3ea53d298a7be5ddb5b4",
        "ba4da2c84d8c3660af565e174f931ed6592e1c76bf551f60b2cba2f664bc982d",
    ),
}
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_BINARY_BYTES = 16 * 1024 * 1024


def _member(tar: tarfile.TarFile, name: str, maximum: int) -> bytes:
    matches = [item for item in tar.getmembers() if item.name.removeprefix("./") == name]
    if len(matches) != 1 or not matches[0].isfile() or not 0 < matches[0].size <= maximum:
        raise ValueError(f"graftcp archive has an invalid {name}")
    stream = tar.extractfile(matches[0])
    if stream is None:
        raise ValueError("graftcp archive member is unreadable")
    with stream:
        return stream.read(maximum + 1)


def _atomic_write(destination: Path, contents: bytes, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as target:
            temporary = Path(target.name)
            target.write(contents)
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install(destination: Path, platform: str = "linux-amd64", *,
            license_directory: Path | None = None) -> None:
    name, archive_hash, binary_hash = ARTIFACTS[platform]
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            with tempfile.TemporaryFile() as archive:
                digest, size = hashlib.sha256(), 0
                with urllib.request.urlopen(BASE + name, timeout=90) as response:
                    for chunk in iter(lambda: response.read(256 * 1024), b""):
                        size += len(chunk)
                        if size > MAX_ARCHIVE_BYTES:
                            raise ValueError("graftcp archive exceeds its size limit")
                        archive.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != archive_hash:
                    raise ValueError("graftcp release archive SHA256 mismatch")
                archive.seek(0)
                with tarfile.open(fileobj=archive, mode="r:gz") as tar:
                    binary = _member(tar, "usr/bin/graftcp", MAX_BINARY_BYTES)
                    license_text = _member(tar, "usr/share/doc/graftcp/COPYING", 128 * 1024)
                if hashlib.sha256(binary).hexdigest() != binary_hash:
                    raise ValueError("graftcp executable SHA256 mismatch")
                if (len(binary) < 20 or binary[:6] != b"\x7fELF\x02\x01"
                        or int.from_bytes(binary[18:20], "little") != 62):
                    raise ValueError("graftcp executable is not Linux amd64 ELF")
                docs = license_directory or destination.parent / "graftcp-license"
                _atomic_write(docs / "COPYING", license_text, 0o644)
                source = (f"graftcp {VERSION}\nUpstream source and GPL-3.0 license:\n"
                          f"https://github.com/hmgle/graftcp/tree/v{VERSION}\n"
                          f"Release archive SHA256: {archive_hash}\n"
                          f"Executable SHA256: {binary_hash}\n").encode()
                _atomic_write(docs / "SOURCE", source, 0o644)
                _atomic_write(destination, binary, 0o755)
                return
        except ValueError:
            raise
        except (OSError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(min(4, 2 ** attempt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=ARTIFACTS, default="linux-amd64")
    parser.add_argument("--destination", type=Path, default=Path("/usr/local/bin/graftcp"))
    parser.add_argument("--license-directory", type=Path,
                        default=Path("/usr/local/share/doc/graftcp"))
    arguments = parser.parse_args()
    install(arguments.destination, arguments.platform, license_directory=arguments.license_directory)
