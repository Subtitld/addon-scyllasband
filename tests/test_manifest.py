"""manifest.json, pins.json and the code agree with each other.

Copyright 2026 Subtitld contributors
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scyllasband_addon import ADDON_ID, __version__, voices  # noqa: E402
from scyllasband_addon.__main__ import DELIVERY, DELIVERY_RANGE  # noqa: E402
from scyllasband_addon.engine import pins  # noqa: E402

MANIFEST = json.loads((REPO_ROOT / 'manifest.json').read_text(encoding='utf-8'))
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_REVISION = re.compile(r'^[0-9a-f]{40}$')


def test_versions_agree():
    pyproject = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    assert MANIFEST['version'] == __version__ == pyproject['project']['version']
    assert re.fullmatch(r'\d\d\.\d\d\.\d+', __version__), 'versions are YY.MM.N'
    assert MANIFEST['id'] == ADDON_ID


def test_manifest_lists_what_the_code_offers():
    assert MANIFEST['languages'] == list(voices.LANGUAGES)
    assert MANIFEST['voices'] == voices.manifest_voices()
    assert MANIFEST['tasks'] == ['tts.synthesize']
    assert MANIFEST['voice_clone'] is False
    assert MANIFEST['compute'] == 'cpu'
    assert 'accelerators' not in MANIFEST
    assert not any(v.get('requires') for v in MANIFEST['voices'])
    assert len(voices.VOICES) == 14 and voices.DEFAULT_VOICE in voices.BY_ID
    assert {v.id for v in voices.VOICES if v.english == 'en_gb'} == {'ink', 'orpheus', 'tuesday'}


def test_options_match_the_code():
    fields = {f['key']: f for f in MANIFEST['config_schema']['fields']}
    assert set(fields) == {*DELIVERY, 'threads'}
    for key in DELIVERY:
        assert fields[key]['type'] == 'number'
        assert (fields[key]['min'], fields[key]['max'], fields[key]['default']) == (*DELIVERY_RANGE, 0)
    assert fields['threads']['type'] == 'int' and fields['threads']['default'] == 0


def test_the_licence_is_spelled_out():
    assert 'Apache-2.0' in MANIFEST['license']
    licences = REPO_ROOT / 'THIRD_PARTY_LICENSES'
    for notice in ('scyllasband/LICENSE', 'litert/LICENSE', 'models/LICENSE', 'models/THIRD_PARTY_NOTICES.md'):
        assert (licences / notice).read_text(encoding='utf-8').strip(), notice
    assert 'Vocos' in (licences / 'models' / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8')


def test_pins_name_one_revision_and_the_whole_bundle():
    data = pins()
    assert data['repo'] == 'spybyscript/scyllasband'
    assert _REVISION.match(data['revision']) and data['tag'].startswith('v3-')
    assert MANIFEST['models'][0]['revision'] == data['revision']
    paths = [pin['path'] for pin in data['files']]
    assert len(paths) == len(set(paths))
    for pin in data['files']:
        assert _SHA256.match(pin['sha256']) and pin['size'] > 0, pin['path']
        assert pin['path'].startswith(data['bundle'] + '/') or pin['path'] in ('LICENSE', 'THIRD_PARTY_NOTICES.md')
    for needed in ('manifest.json', 'assets/voices.json', 'litert/g2p.tflite', 'litert/vocoder.tflite',
                   'litert/vector_estimator.tflite', 'litert/duration_predictor.tflite'):
        assert f'{data["bundle"]}/{needed}' in paths, needed
    # The licence shipped in THIRD_PARTY_LICENSES/models is the pinned one.
    import hashlib
    by_path = {pin['path']: pin for pin in data['files']}
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        text = (REPO_ROOT / 'THIRD_PARTY_LICENSES' / 'models' / name).read_bytes()
        assert hashlib.sha256(text).hexdigest() == by_path[name]['sha256'], name
