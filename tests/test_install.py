"""Offline behavioral tests for release selection, verified caching and relocation."""

import hashlib
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class Releases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='musl action tests ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tag = 'v2.5.0'
        self.calls = []
        self.archives = {}
        for target, triple in installer.TARGETS.items():
            self.archives[target] = self.bundle(triple)
        self.urlopen = patch.object(installer.urllib.request, 'urlopen', side_effect=self.fetch)
        self.urlopen.start()
        self.addCleanup(self.urlopen.stop)

    def bundle(self, triple, wrong_target=False, extra=None):
        stream = io.BytesIO()
        old = '/home/runner/work/musl/musl/dyne/' + triple
        entries = {
            'dyne/gcc-musl/settings.cmake': b'# toolchain\n',
            f'dyne/{triple}/lib/pkgconfig/libcurl.pc': f'prefix={old}\nlibdir=${{prefix}}/lib\nLibs: -L${{libdir}} -lcurl\n'.encode(),
            f'dyne/{triple}/lib/cmake/CURL/CURLConfig.cmake': f'set(PRIVATE "{old}/lib/libz.a")\n'.encode(),
            f'dyne/{triple}/bin/curl-config': f'#!/bin/sh\nprefix="{old}"\necho "$prefix"\n'.encode(),
        }
        for language in ('gcc', 'g++'):
            machine = 'wrong-linux-musl' if wrong_target else triple
            entries[f'dyne/gcc-musl/bin/{triple}-{language}'] = f'#!/bin/sh\necho {machine}\n'.encode()
        if extra:
            entries.update(extra)
        with tarfile.open(fileobj=stream, mode='w:xz') as bundle:
            for name, data in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755 if '/bin/' in name else 0o644
                bundle.addfile(info, io.BytesIO(data))
        return stream.getvalue()

    def release(self):
        names = [f'dyne-gcc-musl-{target}.tar.xz' for target in installer.TARGETS]
        names.append('SHA256SUMS.txt')
        return {'tag_name': self.tag, 'draft': False, 'assets': [
            {'name': name, 'browser_download_url': installer.ASSET_PREFIX + self.tag + '/' + name}
            for name in names]}

    def manifest(self):
        return ''.join(f'{hashlib.sha256(data).hexdigest()}  dyne-gcc-musl-{target}.tar.xz\n'
                       for target, data in self.archives.items()).encode()

    def fetch(self, request, timeout):
        self.calls.append((request.full_url, request.get_header('Authorization')))
        if request.full_url.startswith(installer.API_URL):
            return io.BytesIO(json.dumps(self.release()).encode())
        if request.full_url.endswith('/SHA256SUMS.txt'):
            return io.BytesIO(self.manifest())
        for target, data in self.archives.items():
            if request.full_url.endswith(f'/dyne-gcc-musl-{target}.tar.xz'):
                return io.BytesIO(data)
        raise AssertionError(request.full_url)

    def resolve(self, target='x86_64', version='latest', expected=''):
        return installer.resolve(target, version, expected, self.root, 'private-token')

    def test_all_targets_install_and_export_correct_compilers(self):
        for target, triple in installer.TARGETS.items():
            with self.subTest(target=target):
                _, plan = self.resolve(target)
                outputs = installer.install(plan)
                for language, output in [('gcc', 'CC'), ('g++', 'CXX')]:
                    self.assertTrue(outputs[output].endswith(f'/{triple}-{language}'))
                    self.assertEqual(subprocess.check_output([outputs[output], '-dumpmachine'], text=True).strip(), triple)

    def test_exact_and_legacy_bare_tags_use_returned_release_assets(self):
        for tag in ['v2.5.0', '2.2.0']:
            self.tag = tag
            _, plan = self.resolve(version=tag)
            self.assertEqual(plan['version'], tag)
            self.assertIn('/' + tag + '/', plan['url'])
            self.assertEqual(self.calls[-2][0], installer.API_URL + '/tags/' + tag)

    def test_latest_is_resolved_once_and_token_is_not_sent_to_assets(self):
        _, plan = self.resolve()
        self.tag = 'v2.6.0'
        installer.install(plan)
        self.assertEqual(sum(url.startswith(installer.API_URL) for url, _ in self.calls), 1)
        self.assertEqual(self.calls[0][1], 'Bearer private-token')
        self.assertTrue(all(token is None for _, token in self.calls[1:]))
        self.assertIn('/v2.5.0/', self.calls[-1][0])

    def test_warm_cache_is_verified_without_downloading_again(self):
        _, cold = self.resolve()
        installer.install(cold)
        _, warm = self.resolve()
        installer.install(warm)
        self.assertEqual(cold['cache_key'], warm['cache_key'])
        self.assertNotEqual(cold['install_dir'], warm['install_dir'])
        self.assertEqual(sum(url.endswith(cold['archive']) for url, _ in self.calls), 1)

    def test_release_and_target_changes_do_not_share_caches(self):
        _, first = self.resolve()
        _, other = self.resolve('riscv_64')
        self.tag = 'v2.6.0'
        _, newer = self.resolve()
        self.assertEqual(len({x['cache_key'] for x in [first, other, newer]}), 3)

    def test_bad_cached_archive_fails_before_install(self):
        _, plan = self.resolve()
        cache = Path(plan['cache_dir'])
        cache.mkdir(parents=True)
        (cache / plan['archive']).write_bytes(b'corrupt cache')
        with self.assertRaisesRegex(installer.InstallError, 'Cached archive SHA-256 mismatch'):
            installer.install(plan)
        self.assertFalse(Path(plan['install_dir']).exists())

    def test_bad_download_is_not_extracted_or_cached(self):
        _, plan = self.resolve()
        self.archives['x86_64'] = b'not even a tar file'
        with self.assertRaisesRegex(installer.InstallError, 'Downloaded archive SHA-256 mismatch'):
            installer.install(plan)
        self.assertFalse(Path(plan['install_dir']).exists())
        self.assertEqual(list(Path(plan['cache_dir']).iterdir()), [])

    def test_expected_sha256_pin_is_enforced(self):
        expected = hashlib.sha256(self.archives['x86_64']).hexdigest()
        _, plan = self.resolve(expected=expected.upper())
        self.assertEqual(plan['sha256'], expected)
        with self.assertRaisesRegex(installer.InstallError, 'does not match'):
            self.resolve(expected='0' * 64)

    def test_invalid_inputs_fail_before_network_access(self):
        for target, version, expected in [('arm', 'latest', ''), ('x86_64', '../tag', ''),
                                          ('x86_64', '', ''), ('x86_64', 'latest', 'bad')]:
            with self.subTest(target=target, version=version, expected=expected):
                with self.assertRaises(installer.InstallError):
                    self.resolve(target, version, expected)
        self.assertEqual(self.calls, [])

    def test_missing_and_duplicate_assets_fail(self):
        original = self.release()
        for assets in [[], original['assets'] * 2]:
            with patch.object(self, 'release', return_value={**original, 'assets': assets}):
                with self.assertRaisesRegex(installer.InstallError, 'exactly one'):
                    self.resolve()

    def test_missing_duplicate_and_malformed_manifest_entries_fail(self):
        name = 'dyne-gcc-musl-x86_64.tar.xz'
        for text in ['', f'bad  {name}\n', (f'{"0" * 64}  {name}\n') * 2]:
            with patch.object(self, 'manifest', return_value=text.encode()):
                with self.assertRaisesRegex(installer.InstallError, 'exactly one valid'):
                    self.resolve()

    def test_http_403_and_404_are_clear_and_not_retried(self):
        for code in (403, 404):
            with patch.object(installer.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError(
                    installer.API_URL, code, 'failure', {}, None)) as call:
                with self.assertRaisesRegex(installer.InstallError, f'HTTP {code}'):
                    self.resolve()
                self.assertEqual(call.call_count, 1)

    def test_transient_network_failure_is_retried_with_a_bound(self):
        with patch.object(installer.urllib.request, 'urlopen', side_effect=urllib.error.URLError('timeout')) as call:
            with patch.object(installer.time, 'sleep'):
                with self.assertRaisesRegex(installer.InstallError, 'Network download failed'):
                    self.resolve()
            self.assertEqual(call.call_count, 3)

    def test_archive_path_escape_is_rejected(self):
        self.archives['x86_64'] = self.bundle('x86_64-linux-musl', extra={'dyne/../../escape': b'bad'})
        _, plan = self.resolve()
        with self.assertRaisesRegex(installer.InstallError, 'Unexpected archive path'):
            installer.install(plan)
        self.assertFalse((Path(plan['install_dir']).parent.parent / 'escape').exists())
        self.assertFalse(Path(plan['install_dir']).exists())

    def test_wrong_compiler_target_fails_before_publication(self):
        self.archives['x86_64'] = self.bundle('x86_64-linux-musl', wrong_target=True)
        _, plan = self.resolve()
        with self.assertRaisesRegex(installer.InstallError, 'did not report target'):
            installer.install(plan)
        self.assertFalse(Path(plan['install_dir']).exists())

    def test_existing_installation_is_never_merged_or_deleted(self):
        _, plan = self.resolve()
        root = Path(plan['install_dir'])
        root.mkdir()
        (root / 'keep').write_text('existing')
        with self.assertRaisesRegex(installer.InstallError, 'refusing to mix'):
            installer.install(plan)
        self.assertEqual((root / 'keep').read_text(), 'existing')

    def test_legacy_package_metadata_is_relocated_without_changing_cache(self):
        _, plan = self.resolve()
        installer.install(plan)
        target = Path(plan['install_dir']) / plan['tuple']
        pc = (target / 'lib/pkgconfig/libcurl.pc').read_text()
        cmake = (target / 'lib/cmake/CURL/CURLConfig.cmake').read_text()
        self.assertIn('prefix=${pcfiledir}/../..', pc)
        self.assertNotIn('/home/runner', pc + cmake)
        self.assertIn('${CMAKE_CURRENT_LIST_DIR}/../../../lib/libz.a', cmake)
        config = target / 'bin/curl-config'
        self.assertEqual(subprocess.check_output([config, '--prefix'], text=True).strip(), str(target))
        self.assertEqual(installer.file_digest(Path(plan['cache_dir']) / plan['archive']), plan['sha256'])

    def test_unsupported_host_fails_before_network_access(self):
        with patch.object(installer.platform, 'machine', return_value='aarch64'):
            with self.assertRaisesRegex(installer.InstallError, 'x86_64 Linux'):
                self.resolve()
        self.assertEqual(self.calls, [])

    def test_output_newlines_are_rejected_before_writing(self):
        output = self.root / 'outputs'
        with self.assertRaises(installer.InstallError):
            installer.emit({'CC': 'valid', 'version': 'bad\nvalue'}, output)
        self.assertFalse(output.exists())

    def test_composite_cli_writes_compiler_outputs_and_path_after_success(self):
        output = self.root / 'resolve-output'
        path = self.root / 'github-path'
        env = {'MUSL_TARGET': 'riscv_64', 'MUSL_VERSION': self.tag, 'MUSL_SHA256': '',
               'MUSL_TOKEN': '', 'RUNNER_TEMP': str(self.root),
               'GITHUB_OUTPUT': str(output), 'GITHUB_PATH': str(path)}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(installer.sys, 'argv', ['install.py', 'resolve']):
                self.assertEqual(installer.main(), 0)
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertEqual(values['version'], self.tag)
            installed = self.root / 'install-output'
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(installed)}):
                with patch.object(installer.sys, 'argv', ['install.py', 'install', '--plan', values['plan']]):
                    self.assertEqual(installer.main(), 0)
            outputs = dict(line.split('=', 1) for line in installed.read_text().splitlines())
            self.assertTrue(outputs['CC'].endswith('/riscv64-linux-musl-gcc'))
            self.assertEqual(path.read_text().strip(), str(Path(outputs['CXX']).parent))

    def test_failed_cli_install_does_not_export_compiler_paths(self):
        planfile, plan = self.resolve()
        self.archives['x86_64'] = b'wrong bytes'
        output = self.root / 'output'
        path = self.root / 'path'
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output), 'GITHUB_PATH': str(path)}):
            with patch.object(installer.sys, 'argv', ['install.py', 'install', '--plan', str(planfile)]):
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(installer.main(), 1)
        self.assertIn('SHA-256 mismatch', errors.getvalue())
        self.assertFalse(output.exists())
        self.assertFalse(path.exists())
        self.assertFalse(Path(plan['install_dir']).exists())

    def test_symlink_escape_is_rejected_by_data_filter(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:xz') as bundle:
            info = tarfile.TarInfo('dyne/outside')
            info.type = tarfile.SYMTYPE
            info.linkname = '/tmp/outside'
            bundle.addfile(info)
        self.archives['x86_64'] = stream.getvalue()
        _, plan = self.resolve()
        with self.assertRaises(tarfile.FilterError):
            installer.install(plan)
        self.assertFalse(Path(plan['install_dir']).exists())


if __name__ == '__main__':
    unittest.main()
