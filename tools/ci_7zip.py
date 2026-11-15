"""Install a hash-pinned official 7-Zip for real-codec CI regressions."""
from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import tarfile
import urllib.request
from pathlib import Path

RELEASE = 'https://github.com/ip7z/7zip/releases/download/26.06/'
ASSETS = {
    '7z2606-linux-x64.tar.xz': '2b539cf067b2ad48e1baa5093dbaa8818b73542d1222a0ffde9dc5f2922ea62e',
    '7z2606-mac-x64.tar.xz': '3af68b124052da7fc2a246c96b0bea6a68bfee389baa788f4cead1a133445586',
    '7z2606-mac.tar.xz': 'b6f17920b4240940461dac8ef4009891881ac44d2a5ead625ede2ec8472185dd',
    '7z2606-extra.7z': '396c180f12d2247c24b0b1fcd482c6fc2a2700f61211ffede1061dd971f5e104',
    '7zr.exe': 'e80db2dad715a6c5adabe292d31ed54e11f4e9b99ca349827b50573b175891bc',
}


def main() -> None:
    root = Path(os.environ['RUNNER_TEMP']) / 'codex-exporter-7zip-26.06'
    root.mkdir(parents=True, exist_ok=True)

    def download(name: str) -> Path:
        path = root / name
        request = urllib.request.Request(RELEASE + name, headers={'User-Agent': 'codex-exporter-ci'})
        with urllib.request.urlopen(request, timeout=90) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != ASSETS[name]:
            raise RuntimeError(f'Official 7-Zip asset hash mismatch: {name}')
        path.write_bytes(content)
        return path

    if os.name == 'nt':
        bootstrap = download('7zr.exe')
        archive = download('7z2606-extra.7z')
        subprocess.run([str(bootstrap), 'x', '-y', f'-o{root}', str(archive)], check=True)
        executable = root / 'x64' / '7za.exe'
    else:
        if platform.system() == 'Darwin':
            name = '7z2606-mac.tar.xz' if platform.machine().lower() in {'arm64', 'aarch64'} else '7z2606-mac-x64.tar.xz'
        elif platform.machine().lower() in {'x86_64', 'amd64'}:
            name = '7z2606-linux-x64.tar.xz'
        else:
            raise RuntimeError(f'Unsupported CI architecture: {platform.platform()}')
        archive = download(name)
        executable = root / '7zz'
        with tarfile.open(archive, 'r:xz') as bundle:
            member = next(item for item in bundle.getmembers() if Path(item.name).name == '7zz' and item.isfile())
            content = bundle.extractfile(member)
            if content is None:
                raise RuntimeError('Official archive lacks the 7zz executable')
            with content:
                executable.write_bytes(content.read())
        executable.chmod(0o755)
    if not executable.is_file():
        raise RuntimeError(f'Official 7-Zip executable missing: {executable}')
    subprocess.run([str(executable), 'i'], check=True)
    with Path(os.environ['GITHUB_ENV']).open('a', encoding='utf-8') as stream:
        stream.write(f'CODEX_EXPORT_TEST_7Z={executable}\n')
    with Path(os.environ['GITHUB_PATH']).open('a', encoding='utf-8') as stream:
        stream.write(f'{executable.parent}\n')


if __name__ == '__main__':
    main()
