#!/usr/bin/env python3
"""Exercise published releases, verified archive reuse and relocated consumers."""

import argparse
import importlib.util
import os
from pathlib import Path
import tempfile
import shutil

from smoke import smoke

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def check(work, version, target):
    token = os.environ.get('MUSL_TOKEN', '')
    _, first = installer.resolve(target, version, '', work, token)
    outputs = installer.install(first)
    archive = Path(first['cache_dir']) / first['archive']
    stamp = archive.stat().st_mtime_ns
    # Move an installed SDK again: metadata must stay relative after installation.
    relocated = Path(first['install_dir']).with_name('relocated-dyne')
    Path(first['install_dir']).rename(relocated)
    smoke(relocated, target, relocated.parent / 'cold-build')
    _, second = installer.resolve(target, first['version'], first['sha256'], work, token)
    installer.install(second)
    assert first['cache_key'] == second['cache_key']
    assert stamp == archive.stat().st_mtime_ns, 'Warm install rewrote the cached archive'
    assert installer.file_digest(archive) == first['sha256']
    smoke(Path(second['install_dir']), target, Path(second['install_dir']).parent / 'warm-build')
    print(f'LIVE PASS requested={version} resolved={first["version"]} target={target} '
          f'sha256={first["sha256"]} cold+warm+relocation CC={outputs["CC"]}', flush=True)
    # The shared download cache is sufficient for subsequent cases. Header-heavy
    # SDK copies otherwise exhaust runner disk/inodes during a release matrix.
    shutil.rmtree(Path(first['install_dir']).parent)
    shutil.rmtree(Path(second['install_dir']).parent)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--versions', nargs='+', default=['v2.5.0', 'latest'])
    parser.add_argument('--targets', nargs='+', choices=installer.TARGETS, default=list(installer.TARGETS))
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        for version in args.versions:
            for target in args.targets:
                check(args.work, version, target)
    else:
        with tempfile.TemporaryDirectory(prefix='musl-action-live-') as temporary:
            for version in args.versions:
                for target in args.targets:
                    check(Path(temporary), version, target)
