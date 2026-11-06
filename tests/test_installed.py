"""The add-on as Subtitld runs it: a process spoken to over stdio, speaking for real.

Copyright 2026 Subtitld contributors
SPDX-License-Identifier: Apache-2.0

By default the process is the source tree (`python -m scyllasband_addon`) with
the libraries from tools/build_native.py. With SCYLLASBAND_TEST_BINARY set it
is that executable instead, which is how the release workflow runs these
tests: on the frozen add-on unpacked from the release zip the way Subtitld
installs it, with nothing but what is inside its own bundle.

The model (240 MB) downloads into $SUBTITLD_MODELS_DIR, a temporary folder
when it is not set.
"""

from __future__ import annotations

import array
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scyllasband_addon import engine, voices  # noqa: E402

BINARY = os.environ.get('SCYLLASBAND_TEST_BINARY')
HAVE_RUNTIME = bool(BINARY) or engine.library_path().is_file()

pytestmark = pytest.mark.skipif(not HAVE_RUNTIME, reason='no libscyllasband; run tools/build_native.py')

_MODELS = Path(os.environ.get('SUBTITLD_MODELS_DIR') or tempfile.mkdtemp(prefix='scyllasband-models-'))


def _start(cwd: Path, **extra_env: str) -> subprocess.Popen:
    env = dict(os.environ)
    env['SUBTITLD_MODELS_DIR'] = str(_MODELS)
    for name in list(env):
        if name.startswith('SCYLLASBAND_') and name != 'SCYLLASBAND_TEST_BINARY':
            env.pop(name)
    env.update(extra_env)
    if BINARY:
        env.pop('PYTHONPATH', None)  # the bundle's own code, nothing from this checkout
        command = [BINARY]
    else:
        env['PYTHONPATH'] = str(REPO_ROOT)
        command = [sys.executable, '-m', 'scyllasband_addon']
    # The log goes to a file, not a pipe nobody reads while waiting on stdout:
    # Windows pipes fill after a few KiB and the add-on would block on its log.
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=log)
    proc.log = log
    return proc


def _log(proc) -> str:
    proc.log.seek(0)
    return proc.log.read().decode('utf-8', 'replace')[-20000:]


def _send(proc, frame: dict) -> None:
    proc.stdin.write((json.dumps(frame) + '\n').encode())
    proc.stdin.flush()


def _wait(proc, req_id: str) -> tuple[dict, list[dict]]:
    progress = []
    while True:
        line = proc.stdout.readline()
        assert line, _log(proc)
        frame = json.loads(line)
        if frame.get('id') != req_id:
            continue
        if frame['type'] == 'progress':
            progress.append(frame)
            continue
        return frame, progress


def _say(proc, req_id: str, out: Path, **params) -> tuple[dict, list[dict]]:
    _send(proc, {'id': req_id, 'type': 'tts.synthesize', 'params': {'output_path': str(out), **params}})
    return _wait(proc, req_id)


def _read(path: Path) -> array.array:
    with wave.open(str(path), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        samples = array.array('h', wav.readframes(wav.getnframes()))
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples


def _check(path: Path, at_least_sec: float) -> float:
    samples = _read(path)
    seconds = len(samples) / 24000
    assert seconds >= at_least_sec
    assert max(abs(s) for s in samples) > 1000, 'the audio is (nearly) silent'
    return seconds


def _stop(proc) -> None:
    proc.stdin.close()
    proc.wait(timeout=30)
    proc.stdout.close()
    # The add-on's log, so a failure in CI says what the runtime did.
    sys.stderr.write(_log(proc))
    proc.log.close()


def test_speaks_every_language_in_its_voices(tmp_path):
    proc = _start(tmp_path)
    try:
        hello = json.loads(proc.stdout.readline())
        assert hello['type'] == 'hello' and hello['capabilities'][0]['voice_clone'] is False

        out = tmp_path / 'out' / 'en.wav'
        english = 'The last train leaves at midnight, so pack light. Bring a warm coat, and the map.'
        frame, progress = _say(proc, 'r1', out, text=english, voice='scylla', language='en-US', seed=7)
        assert frame['type'] == 'result', frame
        assert frame['data']['sample_rate'] == 24000 and frame['data']['voice'] == 'scylla'
        assert _check(out, 3.0) == pytest.approx(frame['data']['duration_sec'], abs=0.01)
        assert progress[-1]['value'] == 1.0

        # The same seed gives the same take.
        again = tmp_path / 'out' / 'en-again.wav'
        frame, progress = _say(proc, 'r1b', again, text=english, voice='scylla', language='en', seed=7)
        assert frame['type'] == 'result', frame
        assert not any('Downloading' in p['message'] for p in progress)
        assert _read(again) == _read(out)

        lines = {'es': 'El último tren sale a medianoche.', 'it': "L'ultimo treno parte a mezzanotte.",
                 'fr': 'Le dernier train part à minuit.', 'de': 'Der letzte Zug fährt um Mitternacht.',
                 'vi': 'Chuyến tàu cuối cùng rời ga lúc nửa đêm.'}
        for language, text in lines.items():
            # A folder whose name is not ASCII, and a British voice for good measure.
            target = tmp_path / 'saída' / f'{language}.wav'
            frame, _ = _say(proc, f'r-{language}', target, text=text, voice='ink', language=language)
            assert frame['type'] == 'result', (language, frame)
            _check(target, 1.0)

        frame, _ = _say(proc, 'r-gb', tmp_path / 'out' / 'gb.wav', text='Good evening.', voice='tuesday',
                        language='en-GB')
        assert frame['type'] == 'result', frame
    finally:
        _stop(proc)


def test_delivery_settings_change_the_speech(tmp_path):
    text = 'If we miss it, we walk, and nobody wants to walk that far in the rain.'
    lengths = {}
    for pace in ('-2', '2'):
        proc = _start(tmp_path, SCYLLASBAND_PACE=pace, SCYLLASBAND_INTENSITY='1')
        try:
            proc.stdout.readline()
            out = tmp_path / f'pace{pace}.wav'
            frame, _ = _say(proc, 'p', out, text=text, voice='felix', language='en', seed=3)
            assert frame['type'] == 'result', frame
            lengths[pace] = _check(out, 1.5)
        finally:
            _stop(proc)
    assert lengths['-2'] > lengths['2'] * 1.15, lengths


def test_a_cancel_stops_a_long_line(tmp_path):
    proc = _start(tmp_path)
    try:
        proc.stdout.readline()
        _say(proc, 'warm', tmp_path / 'warm.wav', text='Hello there.', voice='max', language='en')
        long_text = ' '.join(['The rain kept falling on the old station, and nobody came to meet the train.'] * 30)
        _send(proc, {'id': 'long', 'type': 'tts.synthesize',
                     'params': {'output_path': str(tmp_path / 'long.wav'), 'text': long_text,
                                'voice': 'max', 'language': 'en'}})
        while True:  # wait until it is speaking, then cancel
            frame = json.loads(proc.stdout.readline())
            if frame.get('id') == 'long' and frame['type'] == 'progress' and frame['value'] >= 0.9:
                break
        _send(proc, {'type': 'cancel', 'target': 'long'})
        frame, _ = _wait(proc, 'long')
        assert frame['type'] == 'error' and frame['code'] == 'cancelled', frame
        assert not (tmp_path / 'long.wav').exists()
        # The runtime still speaks afterwards.
        frame, _ = _say(proc, 'after', tmp_path / 'after.wav', text='Still here.', voice='max', language='en')
        assert frame['type'] == 'result', frame
    finally:
        _stop(proc)


def test_the_voice_list_matches_the_runtime(monkeypatch):
    # In this process, with the libraries tools/build_native.py left in
    # build/assets/ (the release build job has them too).
    from scyllasband_addon import download
    monkeypatch.setenv('SUBTITLD_MODELS_DIR', str(_MODELS))
    monkeypatch.delenv('SCYLLASBAND_ASSETS', raising=False)
    data = engine.pins()
    download.ensure(data['files'], data['repo'], data['revision'], lambda *_: None, lambda: False)
    runtime = engine.Runtime(engine.bundle_dir())
    try:
        reported = runtime.voices()
    finally:
        runtime.close()
    assert reported['sample_rate'] == voices.SAMPLE_RATE
    by_id = {v['id']: v for v in reported['voices']}
    assert set(by_id) == set(voices.BY_ID)
    for voice in voices.VOICES:
        assert set(by_id[voice.id]['languages']) == {voice.english, *voices.LANGUAGES[1:]}, voice.id
    assert reported['delivery']['ranges'] == {'intensity': [-2.0, 2.0], 'pace': [-2.0, 2.0], 'pitch': [-2.0, 2.0]}
