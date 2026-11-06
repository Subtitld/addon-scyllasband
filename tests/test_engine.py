"""The ctypes binding and the WAV writer.

Copyright 2026 Subtitld contributors
SPDX-License-Identifier: Apache-2.0

The binding tests need the library from tools/build_native.py; they skip
without it (the release workflow's test job runs before any build, and the
installed-archive tests cover the library on every platform).
"""

from __future__ import annotations

import array
import ctypes
import sys
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scyllasband_addon import engine  # noqa: E402


def test_the_structures_match_the_c_abi():
    # Sizes and offsets a C compiler gives include/scyllasband.h on a 64-bit
    # platform (pointers 8 bytes, enums 4).
    assert ctypes.sizeof(engine._RuntimeOptions) == 16
    assert ctypes.sizeof(engine._Request) == 72 and engine._Request.seed.offset == 48
    assert ctypes.sizeof(engine._Event) == 56 and engine._Event.samples.offset == 32


def test_write_wav_clips_and_silences_nan(tmp_path):
    speech = engine.Speech(array.array('f', [0.0, 0.5, -0.5, 1.5, -1.5, float('nan')]), 24000, 0.1)
    out = tmp_path / 'sub' / 'x.wav'
    engine.write_wav(out, speech)
    with wave.open(str(out), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        values = array.array('h', wav.readframes(wav.getnframes()))
    assert list(values) == [0, 16383, -16383, 32767, -32767, 0]
    assert not out.with_name('x.wav.part').exists()


def test_a_missing_library_is_an_engine_error(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, '_lib', None)
    monkeypatch.setenv('SCYLLASBAND_ASSETS', str(tmp_path))
    with pytest.raises(engine.EngineError, match='missing'):
        engine.load_library()


@pytest.mark.skipif(not (REPO_ROOT / 'build' / 'assets' / engine.library_names()[1]).is_file(),
                    reason='no libscyllasband; run tools/build_native.py')
def test_the_built_library_loads_and_reports_abi_3(monkeypatch):
    monkeypatch.delenv('SCYLLASBAND_ASSETS', raising=False)
    monkeypatch.setattr(engine, '_lib', None)
    assert engine.runtime_version() == 'libscyllasband 3.0.0 (litert)'
