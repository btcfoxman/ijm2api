"""Fetch one immutable official CLI artifact during the CI image build only."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
import time
import urllib.request

VERSION = '1.2.0'
# The official front door redirects by region. Pin its verified CDN location
# instead; every source must still match the same immutable release hash.
BASE = ('https://sf16-web-login-neutral.capcutstatic.com/obj/capcut-web-login-static/'
        'videocut/aigc/dreamina_canvas_cli_oversea/1_0_0_26/')
FALLBACK_BASE = 'https://dreamina.capcut.com/canvas-cli/install/1_0_0_26/'
ARTIFACTS = {
    'linux-amd64': ('dreamina-canvas_linux_amd64',
        '2bb3bd62585178017a153dcdaf40991e0ac78a80edbeefebbe611cee193ef649'),
    'windows-amd64': ('dreamina-canvas_windows_amd64.exe',
        '18b6001b23de441b33f955222075f23be841f70dbab0cedb50044850da16b3f8'),
}


def install(destination: Path, platform: str = 'linux-amd64') -> None:
    name, expected = ARTIFACTS[platform]
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(6):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as target:
                temporary = Path(target.name)
                digest = hashlib.sha256()
                base = BASE if attempt % 2 == 0 else FALLBACK_BASE
                with urllib.request.urlopen(base + name, timeout=120) as response:
                    for chunk in iter(lambda: response.read(1024 * 1024), b''):
                        target.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError('official Canvas artifact SHA256 mismatch')
            temporary.chmod(0o755)
            os.replace(temporary, destination)
            return
        except ValueError:
            raise
        except (OSError, TimeoutError):
            if attempt == 5:
                raise
            time.sleep(min(8, 2 ** (attempt // 2)))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=ARTIFACTS, default='linux-amd64')
    parser.add_argument('--destination', type=Path, default=Path('/usr/local/bin/dreamina-canvas'))
    arguments = parser.parse_args()
    install(arguments.destination, arguments.platform)
