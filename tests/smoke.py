#!/usr/bin/env python3
"""Build static C++20/libcurl consumers through both installed package interfaces."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


TARGETS = {'x86_64': 'x86_64-linux-musl', 'arm_64': 'aarch64-linux-musl',
           'arm_hf': 'arm-linux-musleabihf', 'riscv_64': 'riscv64-linux-musl'}
MACHINES = {'x86_64': 'Advanced Micro Devices X86-64', 'arm_64': 'AArch64',
            'arm_hf': 'ARM', 'riscv_64': 'RISC-V'}


def run(*args, env=None):
    subprocess.run([str(arg) for arg in args], check=True, env=env)


def smoke(root, arch, build):
    root = Path(root).resolve()
    triple = TARGETS[arch]
    compiler = root / 'gcc-musl'
    target = root / triple
    sysroot = compiler / triple
    for name in ['curl-config', 'ncursesw6-config']:
        helper = target / 'bin' / name
        if helper.exists():
            reported = subprocess.check_output([helper, '--prefix'], text=True).strip()
            if Path(reported).resolve() != target:
                raise RuntimeError(f'{name} still reports a stale prefix: {reported}')
    source = Path(__file__).with_name('curl')
    # Consume the exported compilers directly: historical settings.cmake files
    # used a different sysroot and hardcoded /opt paths during try_compile.
    args = ['cmake', '-S', source, '-B', build,
            '-DCMAKE_SYSTEM_NAME=Linux', f'-DCMAKE_SYSTEM_PROCESSOR={triple.split("-")[0]}',
            f'-DCMAKE_C_COMPILER={compiler}/bin/{triple}-gcc',
            f'-DCMAKE_CXX_COMPILER={compiler}/bin/{triple}-g++',
            f'-DCMAKE_SYSROOT={sysroot}', f'-DCMAKE_FIND_ROOT_PATH={target};{sysroot}',
            f'-DCMAKE_PREFIX_PATH={target}', '-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER',
            '-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY', '-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY',
            '-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY',
            '-DCMAKE_EXE_LINKER_FLAGS=-static -static-libgcc -static-libstdc++']
    # Use a host cache when available, never a packaged foreign executable.
    if shutil.which('ccache'):
        args += ['-DCMAKE_C_COMPILER_LAUNCHER=ccache', '-DCMAKE_CXX_COMPILER_LAUNCHER=ccache']
    env = dict(os.environ, PKG_CONFIG_LIBDIR=str(target / 'lib/pkgconfig'),
               PKG_CONFIG_PATH='', PKG_CONFIG_SYSROOT_DIR='')
    run(*args, env=env)
    run('cmake', '--build', build, '--parallel', '2')
    for name in ['curl-pkgconfig', 'curl-config']:
        binary = Path(build) / name
        headers = subprocess.check_output([compiler / 'bin' / f'{triple}-readelf', '-h', '-l', '-d', binary], text=True)
        if MACHINES[arch] not in headers or 'INTERP' in headers or 'NEEDED' in headers:
            raise RuntimeError(f'Unexpected ELF architecture or dynamic dependency: {binary}')
        if arch == 'x86_64':
            run(binary)
    print(f'{arch}: static C++20/thread/libcurl pkg-config and CMake consumers passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path, help='Extracted dyne directory')
    parser.add_argument('--target', choices=TARGETS, default='x86_64')
    parser.add_argument('--build', type=Path)
    args = parser.parse_args()
    if args.build:
        smoke(args.root, args.target, args.build)
    else:
        with tempfile.TemporaryDirectory(prefix='musl-action-smoke-') as temporary:
            smoke(args.root, args.target, Path(temporary) / 'build')
