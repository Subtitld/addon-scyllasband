"""tools/check_glibc.py, the gate that keeps the Linux archive on glibc 2.28.

Copyright 2026 Subtitld contributors
SPDX-License-Identifier: Apache-2.0

The ELF file the tests read is the interpreter running them, so what it needs
from glibc is known to be at most this system's glibc.
"""

from __future__ import annotations

import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'tools'))

import check_glibc  # noqa: E402

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='reads a glibc ELF binary')

INTERPRETER = Path(sys.executable).resolve()


def _newest(needs: dict[str, set[str]]) -> str:
    return max((name[len('GLIBC_'):] for name in needs if name != 'GLIBC_PRIVATE'),
               key=check_glibc.version_tuple)


def _bundle(root: Path) -> Path:
    (root / 'bin').mkdir(parents=True)
    shutil.copyfile(INTERPRETER, root / 'bin' / 'program')
    (root / 'manifest.json').write_text('{}', encoding='utf-8')
    return root


def test_reads_what_this_interpreter_needs_from_glibc():
    needs = check_glibc.glibc_needs(INTERPRETER.read_bytes())
    assert needs and all(name.startswith('GLIBC_') for name in needs)
    assert any(needs.values()), 'no symbol was matched to its version'
    host = os.confstr('CS_GNU_LIBC_VERSION').split()[1]
    assert check_glibc.version_tuple(_newest(needs)) <= check_glibc.version_tuple(host)


def test_a_newer_glibc_than_the_floor_fails_and_names_the_symbols(tmp_path):
    bundle = _bundle(tmp_path / 'bundle')
    needs = check_glibc.glibc_needs(INTERPRETER.read_bytes())
    newest = _newest(needs)

    assert check_glibc.check([bundle], newest) == (1, newest, [])

    count, found, problems = check_glibc.check([bundle], '2.0')
    assert (count, found) == (1, newest)
    assert len(problems) == len(needs)
    assert all(problem.startswith('bin/program needs GLIBC_') for problem in problems)
    version, symbols = next((v, s) for v, s in sorted(needs.items()) if s)
    line = next(problem for problem in problems if problem.startswith(f'bin/program needs {version},'))
    assert sorted(symbols)[0] in line


def test_the_archive_is_read_like_the_folder(tmp_path):
    bundle = _bundle(tmp_path / 'bundle')
    archive = tmp_path / 'addon.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        for path in sorted(bundle.rglob('*')):
            zf.write(path, path.relative_to(bundle).as_posix())
    assert check_glibc.check([archive], '2.0') == check_glibc.check([bundle], '2.0')
    assert check_glibc.main([str(archive), '--max', '2.0']) == 1
    assert check_glibc.main([str(archive), '--max', '999']) == 0


def test_glibc_markers_and_private_versions(tmp_path, monkeypatch):
    (tmp_path / 'libmvec.so.1').write_bytes(b'\x7fELF')
    monkeypatch.setattr(check_glibc, 'glibc_needs', lambda data: {
        'GLIBC_2.2.5': {'memcpy'}, 'GLIBC_ABI_DT_RELR': set(), 'GLIBC_PRIVATE': {'__libc_x'}, 'GLIBC_FUTURE': set()})
    count, newest, problems = check_glibc.check([tmp_path], '2.36')
    assert (count, newest) == (1, '2.36')
    assert problems == ['libmvec.so.1 needs GLIBC_FUTURE: a glibc version this check does not know',
                        'libmvec.so.1 needs GLIBC_PRIVATE: a part of glibc itself, which works only with the '
                        'glibc it came from']
    problems = check_glibc.check([tmp_path], '2.28')[2]
    assert ('libmvec.so.1 needs GLIBC_ABI_DT_RELR (glibc 2.36), newer than 2.28: no symbol, but the dynamic '
            'linker checks it') in problems


def test_what_cannot_be_read_fails(tmp_path):
    broken = tmp_path / 'broken'
    broken.mkdir()
    (broken / 'lib.so').write_bytes(INTERPRETER.read_bytes()[:64])  # the header, no sections
    count, _, problems = check_glibc.check([broken])
    assert count == 1 and len(problems) == 1 and problems[0].startswith('lib.so: truncated or malformed')

    empty = tmp_path / 'empty'
    empty.mkdir()
    assert check_glibc.check([empty])[2] == [f'no ELF file found in {empty}']
    assert check_glibc.check([tmp_path / 'absent'])[2] == [f'{tmp_path / "absent"}: does not exist']
