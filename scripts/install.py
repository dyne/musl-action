#!/usr/bin/env python3
"""Resolve and install a checksum-verified public dyne/musl release."""

import argparse
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


API_URL = 'https://api.github.com/repos/dyne/musl/releases'
ASSET_PREFIX = 'https://github.com/dyne/musl/releases/download/'
TARGETS = {
    'x86_64': 'x86_64-linux-musl',
    'arm_64': 'aarch64-linux-musl',
    'arm_hf': 'arm-linux-musleabihf',
    'riscv_64': 'riscv64-linux-musl',
}
SCHEMA = 1


class InstallError(Exception):
    """An actionable installer failure, suitable for a CI diagnostic."""


def check_host():
    if sys.version_info < (3, 12):
        raise InstallError('Python 3.12 or newer is required')
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        raise InstallError('Dyne compilers require an x86_64 Linux runner')


def request(url, consume, token=''):
    """Retry transient GET failures; the caller supplies tokens only for the API."""
    headers = {'User-Agent': 'dyne-musl-action'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                return consume(response)
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise InstallError(f'HTTP {error.code} fetching {url}') from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            if attempt == 2:
                reason = error.reason if isinstance(error, urllib.error.URLError) else error
                raise InstallError(f'Network download failed for {url}: {reason}') from error
        time.sleep(attempt + 1)


def checksum(manifest, archive):
    matches = []
    for line in manifest.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].removeprefix('*') == archive:
            matches.append(fields[0])
    if len(matches) != 1 or not re.fullmatch(r'[0-9a-fA-F]{64}', matches[0]):
        raise InstallError(f'Manifest must contain exactly one valid SHA-256 for {archive}')
    return matches[0].lower()


def asset_url(release, name):
    assets = [asset for asset in release['assets'] if asset['name'] == name]
    if len(assets) != 1:
        raise InstallError(f'Release {release["tag_name"]} must contain exactly one {name}')
    url = assets[0]['browser_download_url']
    prefix = ASSET_PREFIX + urllib.parse.quote(release['tag_name'], safe='') + '/'
    if url != prefix + name:
        raise InstallError(f'Unexpected release asset URL for {name}')
    return url


def emit(values, output):
    for key, value in values.items():
        if '\n' in str(value) or '\r' in str(value):
            raise InstallError(f'Invalid multiline output: {key}')
    with Path(output).open('a', encoding='utf-8') as stream:
        for key, value in values.items():
            stream.write(f'{key}={value}\n')


def resolve(target, version, expected, runner_temp, token=''):
    check_host()
    if target not in TARGETS:
        raise InstallError(f'Unsupported architecture: {target}')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._+-]{0,127}', version):
        raise InstallError('version must be an exact release tag or latest')
    if expected and not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
        raise InstallError('sha256 must contain exactly 64 hexadecimal characters')
    endpoint = '/latest' if version == 'latest' else '/tags/' + urllib.parse.quote(version, safe='')
    release = json.loads(request(API_URL + endpoint, lambda response: response.read(), token))
    tag = release['tag_name']
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._+-]{0,127}', tag):
        raise InstallError('Release API returned an unsupported tag')
    if release.get('draft') or (version != 'latest' and tag != version):
        raise InstallError('Release API did not return the requested published release')
    archive = f'dyne-gcc-musl-{target}.tar.xz'
    url = asset_url(release, archive)
    manifest_url = asset_url(release, 'SHA256SUMS.txt')
    manifest = request(manifest_url, lambda response: response.read(1024 * 1024).decode('utf-8'))
    digest = checksum(manifest, archive)
    if expected and expected.lower() != digest:
        raise InstallError('Expected sha256 does not match the release manifest; nothing installed')
    cache_key = f'musl-action-v{SCHEMA}-Linux-X64-{target}-{tag}-{digest}'
    root = Path(runner_temp).resolve()
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='musl-action-', dir=root))
    plan = {
        'version': tag, 'target': target, 'tuple': TARGETS[target], 'sha256': digest,
        'archive': archive, 'url': url, 'cache_key': cache_key,
        'cache_dir': str(root / 'musl-action-downloads' / cache_key),
        'install_dir': str(work / 'dyne'),
    }
    plan_path = work / 'plan.json'
    plan_path.write_text(json.dumps(plan, indent=2) + '\n', encoding='utf-8')
    return plan_path, plan


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verified_archive(plan):
    cache = Path(plan['cache_dir'])
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / plan['archive']
    if archive.exists():
        if file_digest(archive) != plan['sha256']:
            raise InstallError('Cached archive SHA-256 mismatch; remove this cache and retry')
        return archive
    with tempfile.TemporaryDirectory(prefix='download-', dir=cache) as temporary:
        download = Path(temporary) / plan['archive']

        def consume(response):
            with download.open('wb') as stream:
                shutil.copyfileobj(response, stream)

        request(plan['url'], consume)
        if file_digest(download) != plan['sha256']:
            raise InstallError('Downloaded archive SHA-256 mismatch; nothing installed')
        download.replace(archive)
    return archive


def validate_toolchain(stage, target):
    if not (stage / 'gcc-musl/settings.cmake').is_file():
        raise InstallError('Archive is missing gcc-musl/settings.cmake')
    for language in ('gcc', 'g++'):
        compiler = stage / 'gcc-musl/bin' / f'{target}-{language}'
        if not compiler.is_file() or not os.access(compiler, os.X_OK):
            raise InstallError(f'Archive is missing executable {compiler.name}')
        result = subprocess.run([str(compiler), '-dumpmachine'], capture_output=True, text=True, timeout=30)
        if result.returncode or result.stdout.strip() != target:
            raise InstallError(f'{compiler.name} did not report target {target}: {result.stderr.strip()}')


def install(plan):
    check_host()
    destination = Path(plan['install_dir'])
    if destination.exists() or destination.is_symlink():
        raise InstallError('Installation destination already exists; refusing to mix toolchains')
    archive = verified_archive(plan)
    with tempfile.TemporaryDirectory(prefix='extract-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        with tarfile.open(archive, 'r:xz') as bundle:
            for member in bundle.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'dyne':
                    raise InstallError(f'Unexpected archive path: {member.name}')
            bundle.extractall(staging, filter='data')
        stage = staging / 'dyne'
        validate_toolchain(stage, plan['tuple'])
        # Older published archives contain build-host paths in curl/SSH2 metadata.
        # Normalize the verified extraction; the cached release bytes stay intact.
        spec = importlib.util.spec_from_file_location('relocate_metadata', Path(__file__).with_name('relocate-metadata.py'))
        relocator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(relocator)
        changed = relocator.relocate(stage / plan['tuple'])
        if changed:
            print(f'Relocated {len(changed)} package metadata files')
        stage.rename(destination)
    compiler = destination / 'gcc-musl/bin' / plan['tuple']
    return {'CC': str(compiler) + '-gcc', 'CXX': str(compiler) + '-g++'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('resolve')
    installer = subparsers.add_parser('install')
    installer.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'resolve':
            path, plan = resolve(os.environ.get('MUSL_TARGET', 'x86_64'),
                                 os.environ.get('MUSL_VERSION', 'latest'),
                                 os.environ.get('MUSL_SHA256', ''), os.environ['RUNNER_TEMP'],
                                 os.environ.get('MUSL_TOKEN', ''))
            emit({key: plan[key] for key in ('version', 'sha256', 'cache_key', 'cache_dir')} |
                 {'plan': str(path)}, os.environ['GITHUB_OUTPUT'])
            print(f'Resolved dyne/musl {plan["version"]} {plan["target"]}, SHA256 {plan["sha256"]}')
        else:
            plan = json.loads(args.plan.read_text(encoding='utf-8'))
            outputs = install(plan)
            emit(outputs, os.environ['GITHUB_OUTPUT'])
            with Path(os.environ['GITHUB_PATH']).open('a', encoding='utf-8') as stream:
                stream.write(str(Path(outputs['CC']).parent) + '\n')
            print(f'Installed dyne/musl {plan["version"]} {plan["target"]} into {plan["install_dir"]}')
    except (InstallError, OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f'musl-action: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
