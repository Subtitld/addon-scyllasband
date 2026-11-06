#!/usr/bin/env python3
"""Build libscyllasband and gather it, with LiteRT, into build/assets/.

Copyright 2026 Subtitld contributors
SPDX-License-Identifier: Apache-2.0

libscyllasband is the native runtime of Scylla's Band (lowkeytea/scyllasband,
directory libscyllasband/, Apache-2.0), built unmodified at the commit pinned
below with its LiteRT backend. LiteRT is Google's prebuilt 2.2.0 runtime,
downloaded from Google's bucket and checked against the sha256 pinned here.

    python tools/build_native.py            # needs git, cmake and a C++17 compiler

Output, all of it copied into the frozen add-on:

    build/assets/libLiteRt.so          libscyllasband_native.so        (Linux)
    build/assets/libLiteRt.dylib       libscyllasband_native.dylib     (macOS)
    build/assets/libLiteRt.dll         scyllasband_native.dll          (Windows)

On Linux the C++ runtime is linked statically (-static-libstdc++
-static-libgcc), so the library needs only glibc; LiteRT's prebuilt needs glibc
2.26. On Windows the C runtime is linked statically (/MT), and LiteRT, which
Google ships without an import library, gets one made from its export table.
On macOS the library looks for LiteRT beside itself (@loader_path) and is
re-signed ad hoc after that change.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD = REPO_ROOT / 'build'
ASSETS = BUILD / 'assets'
SOURCE = BUILD / 'scyllasband-src'

SCYLLASBAND_REPO = 'https://github.com/lowkeytea/scyllasband'
SCYLLASBAND_COMMIT = 'e2574d7b6e8947577a7830010ac325e6172f3951'  # main, 2026-11-04: ABI 3.0.0, contract v3

LITERT_VERSION = '2.2.0'
LITERT_URL = 'https://storage.googleapis.com/litert/binaries/{version}/{platform}/{name}'
# platform label -> (Google's folder, file, sha256)
LITERT = {
    'linux-x86_64': ('linux_x86_64', 'libLiteRt.so',
                     '5f91e38c8f4ba0629a5239cdd3c7ec3fc8606fc89f7c6727e937db2c21e573ab'),
    'macos-arm64': ('macos_arm64', 'libLiteRt.dylib',
                    '6f5b51140d9d3353e9b63bc1b84f50a3eeaad944d617f07d74a27f62904618ca'),
    'windows-x86_64': ('windows_x86_64', 'libLiteRt.dll',
                       '3f2b6ed9ccca4f8d8a298a9b49006fcf9b176ef27ea2c757d1ce552b736a5675'),
}


def run(cmd: list, cwd: Path | None = None) -> None:
    print('+', ' '.join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def platform_label() -> str:
    machine = platform.machine().lower()
    if sys.platform.startswith('linux') and machine in ('x86_64', 'amd64'):
        return 'linux-x86_64'
    if sys.platform == 'darwin' and machine in ('arm64', 'aarch64'):
        return 'macos-arm64'
    if sys.platform == 'win32' and machine in ('amd64', 'x86_64'):
        return 'windows-x86_64'
    raise SystemExit(f'unsupported build platform: {sys.platform} {machine}')


def checkout() -> None:
    if not (SOURCE / '.git').exists():
        SOURCE.mkdir(parents=True, exist_ok=True)
        run(['git', 'init', '-q'], cwd=SOURCE)
        run(['git', 'remote', 'add', 'origin', SCYLLASBAND_REPO], cwd=SOURCE)
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=SOURCE, capture_output=True, text=True).stdout.strip()
    if head != SCYLLASBAND_COMMIT:
        run(['git', 'fetch', '-q', '--depth', '1', 'origin', SCYLLASBAND_COMMIT], cwd=SOURCE)
        run(['git', 'checkout', '-q', '--force', SCYLLASBAND_COMMIT], cwd=SOURCE)
    run(['git', 'reset', '-q', '--hard'], cwd=SOURCE)
    run(['git', 'clean', '-q', '-fdx', 'libscyllasband/third_party'], cwd=SOURCE)


def fetch_litert(label: str) -> Path:
    folder, name, sha256 = LITERT[label]
    staged = SOURCE / 'libscyllasband' / 'third_party' / 'litert' / 'lib' / label
    staged.mkdir(parents=True, exist_ok=True)
    target = staged / name
    if not (target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == sha256):
        url = LITERT_URL.format(version=LITERT_VERSION, platform=folder, name=name)
        print('fetching', url, flush=True)
        request = urllib.request.Request(url, headers={'User-Agent': 'subtitld-scyllasband-build/1'})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
        got = hashlib.sha256(data).hexdigest()
        if got != sha256:
            raise SystemExit(f'FATAL: {url} has sha256 {got}, pinned {sha256}')
        target.write_bytes(data)
    return target


# ---------------------------------------------------------------------------
# Windows: an import library for libLiteRt.dll, from its export table
# ---------------------------------------------------------------------------
def pe_exports(path: Path) -> list[str]:
    """The names a PE32+ DLL exports."""
    data = path.read_bytes()
    pe = struct.unpack_from('<I', data, 0x3C)[0]
    if data[pe:pe + 4] != b'PE\0\0':
        raise SystemExit(f'{path} is not a PE file')
    sections_count = struct.unpack_from('<H', data, pe + 6)[0]
    optional_size = struct.unpack_from('<H', data, pe + 20)[0]
    optional = pe + 24
    if struct.unpack_from('<H', data, optional)[0] != 0x20B:
        raise SystemExit(f'{path} is not PE32+')
    export_rva = struct.unpack_from('<I', data, optional + 112)[0]
    sections = []
    for i in range(sections_count):
        base = optional + optional_size + 40 * i
        size, rva, raw_size, raw_pointer = struct.unpack_from('<IIII', data, base + 8)
        sections.append((rva, max(size, raw_size), raw_pointer))

    def offset(rva: int) -> int:
        for start, size, raw in sections:
            if start <= rva < start + size:
                return rva - start + raw
        raise SystemExit(f'{path}: RVA {rva:#x} is in no section')

    directory = offset(export_rva)
    count, names_rva = struct.unpack_from('<I', data, directory + 24)[0], struct.unpack_from('<I', data, directory + 32)[0]
    names = []
    for i in range(count):
        name_rva = struct.unpack_from('<I', data, offset(names_rva) + 4 * i)[0]
        start = offset(name_rva)
        names.append(data[start:data.index(b'\0', start)].decode('ascii'))
    return names


def import_library(dll: Path) -> Path:
    exports = pe_exports(dll)
    definition = dll.with_suffix('.def')
    definition.write_text(f'LIBRARY {dll.name}\nEXPORTS\n' + ''.join(f'  {name}\n' for name in exports),
                          encoding='ascii')
    library = dll.with_suffix('.lib')
    run(['lib', '/nologo', f'/def:{definition}', f'/out:{library}', '/machine:x64'])
    print(f'{library.name}: {len(exports)} exports', flush=True)
    return library


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build(label: str, litert: Path) -> Path:
    tree = BUILD / 'cmake'
    args = ['cmake', '-S', SOURCE / 'libscyllasband', '-B', tree, '-DCMAKE_BUILD_TYPE=Release',
            '-DSCYLLASBAND_BACKEND=litert', '-DSCYLLASBAND_BUILD_TESTS=OFF', '-DSCYLLASBAND_BUILD_TOOLS=ON']
    if label == 'linux-x86_64':
        args.append('-DCMAKE_SHARED_LINKER_FLAGS=-static-libstdc++ -static-libgcc')
        args.append('-DCMAKE_EXE_LINKER_FLAGS=-static-libstdc++ -static-libgcc')
    elif label == 'macos-arm64':
        args.append('-DCMAKE_OSX_DEPLOYMENT_TARGET=12.0')
    elif label == 'windows-x86_64':
        args += [f'-DSCYLLASBAND_LITERT_LIBRARY={import_library(litert)}',
                 '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded', '-DCMAKE_POLICY_DEFAULT_CMP0091=NEW']
    run(args)
    run(['cmake', '--build', tree, '--config', 'Release', '--parallel'])
    names = {'linux-x86_64': 'libscyllasband_native.so', 'macos-arm64': 'libscyllasband_native.dylib',
             'windows-x86_64': 'scyllasband_native.dll'}
    candidates = [p for p in tree.rglob(names[label]) if p.is_file()]
    if not candidates:
        raise SystemExit(f'FATAL: the build made no {names[label]}')
    return candidates[0].resolve()


def install(label: str, library: Path, litert: Path) -> None:
    if ASSETS.exists():
        shutil.rmtree(ASSETS)
    ASSETS.mkdir(parents=True)
    names = {'linux-x86_64': 'libscyllasband_native.so', 'macos-arm64': 'libscyllasband_native.dylib',
             'windows-x86_64': 'scyllasband_native.dll'}
    native = ASSETS / names[label]
    shutil.copyfile(library, native)
    shutil.copyfile(litert, ASSETS / litert.name)
    if label == 'linux-x86_64':
        if shutil.which('patchelf'):
            run(['patchelf', '--set-rpath', '$ORIGIN', native])
        else:
            print('patchelf not found: the library keeps the build RUNPATH; the add-on preloads LiteRT anyway')
    elif label == 'macos-arm64':
        listing = subprocess.run(['otool', '-L', native], capture_output=True, text=True, check=True).stdout
        for line in listing.splitlines()[1:]:
            path = line.strip().split(' ', 1)[0]
            if path.endswith('libLiteRt.dylib') and path != '@loader_path/libLiteRt.dylib':
                run(['install_name_tool', '-change', path, '@loader_path/libLiteRt.dylib', native])
        run(['install_name_tool', '-id', '@rpath/libscyllasband_native.dylib', native])
        run(['codesign', '--force', '--sign', '-', native])
    print('assets:', ', '.join(sorted(p.name for p in ASSETS.iterdir())), flush=True)


def main() -> int:
    label = platform_label()
    checkout()
    litert = fetch_litert(label)
    library = build(label, litert)
    install(label, library, litert)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
