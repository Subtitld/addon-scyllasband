"""Scylla's Band add-on for Subtitld — JSON-line protocol entry point.

Copyright 2026 Subtitld contributors
SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the "License"); you may not
use this file except in compliance with the License. You may obtain a copy of
the License at http://www.apache.org/licenses/LICENSE-2.0. Unless required by
applicable law or agreed to in writing, software distributed under the License
is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.

What this add-on is
-------------------
Offline text-to-speech with Scylla's Band: fourteen voices, each speaking
English, Spanish, Italian, French, German and Vietnamese at 24 kHz, with how
intense, how fast and how high they speak set in Configure. Serves
`tts.synthesize`. The model's own C++ runtime does the synthesis, loaded
in-process (engine.py). The model, about 240 MB, downloads on the first line
(download.py); after that nothing leaves the machine.

Wire format
-----------
One JSON object per line on stdin/stdout. Stderr is free-form logging, which
the host prefixes with `[addon:scyllasband]` and surfaces.

Options (the host sets them from Configure as SCYLLASBAND_<KEY>)
----------------------------------------------------------------
intensity, pace, pitch: −2 to 2, 0 being each voice's own delivery; threads:
0 for the runtime's default.

Limits
------
One line at a time. A cancel stops the line at the runtime's next graph call,
or stops the download (which resumes on the next request).
"""

from __future__ import annotations

import errno
import json
import logging
import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path

from scyllasband_addon import ADDON_ID, PROTOCOL_VERSION, __version__
from scyllasband_addon import download, engine, tls, voices

ADDON_VERSION = __version__

ERR_MODEL_MISSING = 'model_missing'
ERR_UNSUPPORTED_LANGUAGE = 'unsupported_language'
ERR_UNSUPPORTED_VOICE = 'unsupported_voice'
ERR_BAD_PARAMS = 'bad_params'
ERR_INTERNAL = 'internal'
ERR_CANCELLED = 'cancelled'
ERR_DISK_FULL = 'disk_full'
ERR_NETWORK = 'network_unavailable'

# No space left on the device, or over the user's disk quota.
_DISK_FULL_ERRNOS = {errno.ENOSPC, getattr(errno, 'EDQUOT', 122)}

DELIVERY = ('intensity', 'pace', 'pitch')
DELIVERY_RANGE = (-2.0, 2.0)

log = logging.getLogger('scyllasband_addon')


# ---------------------------------------------------------------------------
# stdio ownership + protocol frames
# ---------------------------------------------------------------------------
_PROTO_IN = None   # binary reader for host frames
_PROTO_OUT = None  # binary writer for our frames
_proto_lock = threading.Lock()


def _claim_protocol_stdio() -> None:
    """Give the protocol exclusive use of the real stdin/stdout.

    fd-level, not just `sys.stdout = sys.stderr`, so nothing else can write a
    stray line into the protocol: the native runtime and LiteRT print to
    stdout ("Created TensorFlow Lite XNNPACK delegate"), which now goes to
    stderr, the log. Falls back to the Python-level swap if the fd dance fails.
    """
    global _PROTO_IN, _PROTO_OUT
    try:
        sys.stdout.flush()
        in_fd = os.dup(0)
        out_fd = os.dup(1)
        devnull = os.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(devnull, 0)
        finally:
            os.close(devnull)
        os.dup2(2, 1)
        _PROTO_IN = os.fdopen(in_fd, 'rb')
        _PROTO_OUT = os.fdopen(out_fd, 'wb')
    except (OSError, ValueError, AttributeError):
        _PROTO_IN = sys.stdin.buffer
        _PROTO_OUT = sys.stdout.buffer
    sys.stdin = open(os.devnull, encoding='utf-8')  # stray input() -> EOFError
    sys.stdout = sys.stderr                        # stray print() -> the log


def _emit(frame: dict) -> None:
    data = (json.dumps(frame, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    out = _PROTO_OUT if _PROTO_OUT is not None else sys.stdout.buffer
    with _proto_lock:
        try:
            out.write(data)
            out.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # host went away; the frame loop exits on the next empty read


def _emit_progress(req_id: str, value: float, message: str = '') -> None:
    _emit({'id': req_id, 'type': 'progress',
           'value': max(0.0, min(1.0, float(value))), 'message': message})


def _emit_result(req_id: str, data: dict) -> None:
    _emit({'id': req_id, 'type': 'result', 'data': data})


def _emit_error(req_id: str, code: str, message: str, retryable: bool = False) -> None:
    _emit({'id': req_id, 'type': 'error', 'code': code,
           'message': message, 'retryable': retryable})


class _AddonError(Exception):
    """An expected failure that already knows its protocol code."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
def _env(*names: str) -> str:
    """First non-empty value among `names`, stripped. '' when none are set."""
    for name in names:
        value = (os.environ.get(name) or '').strip()
        if value:
            return value
    return ''


def _number(name: str, low: float, high: float, default: float, sent=None) -> float:
    raw = str(sent).strip() if sent not in (None, '') else _env(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning('%s=%r is not a number; using %s', name, raw, default)
        return default
    if value != value:  # NaN
        return default
    return max(low, min(high, value))


def _options(sent: dict | None = None) -> dict:
    """Configure's values: from the request's `options` when the host sends
    them (it does for speech-to-text), else from the environment it set at
    spawn, else the defaults."""
    sent = sent if isinstance(sent, dict) else {}
    options = {key: _number(f'SCYLLASBAND_{key.upper()}', *DELIVERY_RANGE, 0.0, sent.get(key))
               for key in DELIVERY}
    options['threads'] = int(_number('SCYLLASBAND_THREADS', 0, 256, 0, sent.get('threads')))
    return options


def delivery_spec(options: dict) -> str:
    """The runtime's delivery string, e.g. `intensity=1,pace=-0.5,pitch=0`; '' for the voice's own."""
    if not any(options[key] for key in DELIVERY):
        return ''
    return ','.join(f'{key}={options[key]:g}' for key in DELIVERY)


def _resolve_language(raw) -> str:
    """The primary tag Scylla's Band speaks (`en-GB`, `es_419`, `vi-VN` -> en, es, vi)."""
    tag = str(raw or '').strip().lower().replace('_', '-')
    primary = tag.split('-', 1)[0]
    if primary in voices.LANGUAGES:
        return primary
    spoken = ', '.join(voices.LANGUAGE_NAMES[code] for code in voices.LANGUAGES)
    if not primary:
        raise _AddonError(ERR_UNSUPPORTED_LANGUAGE,
                          f"Set the project's language first: Scylla's Band speaks {spoken}.")
    raise _AddonError(ERR_UNSUPPORTED_LANGUAGE,
                      f"Scylla's Band does not speak {raw!r}. It speaks {spoken}; "
                      f'pick another voice engine for other languages.')


def _seed(params: dict) -> int:
    try:
        return int(params.get('seed', 7)) & 0x7FFFFFFF
    except (TypeError, ValueError):
        return 7


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------
class _Request:
    def __init__(self, req_id: str):
        self.req_id = req_id
        self.cancel = threading.Event()
        self.speaking = False
        self.thread: threading.Thread | None = None


_active: dict[str, _Request] = {}
_active_lock = threading.Lock()
_work_lock = threading.Lock()  # one line, or one download, at a time


def _ensure_model(state: _Request) -> None:
    data = engine.pins()
    todo = download.missing(data['files'], data['revision'])
    if not todo:
        return
    total = sum(pin['size'] for pin in todo)
    message = f"Downloading the Scylla's Band model ({total / 1e6:,.0f} MB)..."
    _emit_progress(state.req_id, 0.0, message)
    last = [0.0]

    def progress(done: int, total_bytes: int) -> None:
        now = time.monotonic()
        if now - last[0] >= 0.25 or done >= total_bytes:
            last[0] = now
            _emit_progress(state.req_id, 0.85 * done / max(1, total_bytes), message)

    started = time.monotonic()
    download.ensure(data['files'], data['repo'], data['revision'], progress, state.cancel.is_set)
    log.info('downloaded %.0f MB in %.1fs', total / 1e6, time.monotonic() - started)


def _handle_synthesize(state: _Request, params: dict) -> None:
    req_id = state.req_id
    text = ' '.join(str(params.get('text') or '').split())
    output_path = params.get('output_path')
    if not text:
        _emit_error(req_id, ERR_BAD_PARAMS, 'text is empty')
        return
    if not isinstance(output_path, str) or not output_path.strip():
        _emit_error(req_id, ERR_BAD_PARAMS, 'output_path is required')
        return
    options = _options(params.get('options'))
    try:
        language = _resolve_language(params.get('language'))
        voice_id = str(params.get('voice') or '') or voices.DEFAULT_VOICE
        if voice_id not in voices.BY_ID:
            raise _AddonError(ERR_UNSUPPORTED_VOICE, f'unknown voice {voice_id!r}')
    except _AddonError as exc:
        _emit_error(req_id, exc.code, exc.message)
        return
    voice = voices.BY_ID[voice_id]
    locale = voices.runtime_language(language, voice)
    delivery = delivery_spec(options)
    seed = _seed(params)

    outcome: tuple = ('error', ERR_INTERNAL, 'no outcome', False)
    try:
        if state.cancel.is_set():
            raise download.Cancelled()
        with _work_lock:
            _ensure_model(state)
            if state.cancel.is_set():
                raise download.Cancelled()
            _emit_progress(req_id, 0.88, 'Loading the voices...')
            runtime = engine.runtime(engine.bundle_dir(), options['threads'])
            _emit_progress(req_id, 0.9, 'Speaking...')
            log.info('synthesize %s: voice=%s language=%s delivery=%s seed=%d chars=%d',
                     req_id, voice_id, locale, delivery or 'default', seed, len(text))
            state.speaking = True
            try:
                speech = runtime.synthesize(
                    text, voice_id, locale, delivery, seed,
                    progress=lambda fraction: _emit_progress(req_id, 0.9 + 0.09 * fraction, 'Speaking...'),
                    cancel=state.cancel)
            finally:
                state.speaking = False
            engine.write_wav(Path(output_path), speech)
        log.info('synthesize %s: %.2fs of audio in %.2fs', req_id, speech.duration_sec, speech.seconds)
        outcome = ('result', {
            'path': output_path,
            'duration_sec': round(speech.duration_sec, 3),
            'sample_rate': speech.sample_rate,
            'channels': 1,
            'voice': voice_id,
        })
    except (download.Cancelled, engine.Cancelled):
        log.info('synthesize %s: cancelled', req_id)
        outcome = ('error', ERR_CANCELLED, 'cancelled', False)
    except download.DownloadError as exc:
        log.warning('synthesize %s: download failed: %s', req_id, exc)
        outcome = ('error', ERR_NETWORK, f'Could not download the model: {exc}', True)
    except engine.EngineError as exc:
        log.warning('synthesize %s failed: %s', req_id, exc)
        outcome = ('error', ERR_INTERNAL, str(exc), False)
    except OSError as exc:
        log.exception('synthesize %s failed', req_id)
        if getattr(exc, 'errno', None) in _DISK_FULL_ERRNOS:
            outcome = ('error', ERR_DISK_FULL, f'Out of disk space: {exc}', False)
        else:
            outcome = ('error', ERR_INTERNAL, f'{type(exc).__name__}: {exc}', False)
    except Exception as exc:  # noqa: BLE001 — every path must end in one terminal frame
        log.exception('synthesize %s failed', req_id)
        outcome = ('error', ERR_INTERNAL, f'{type(exc).__name__}: {exc}', False)

    if outcome[0] == 'result':
        _emit_progress(req_id, 1.0, '')
        _emit_result(req_id, outcome[1])
    else:
        _emit_error(req_id, *outcome[1:])


def _cancel(state: _Request) -> None:
    state.cancel.set()
    if state.speaking:
        runtime = engine.current()
        if runtime is not None:
            runtime.cancel()


def _spawn(req_id: str, target, *args) -> None:
    state = _Request(req_id)
    with _active_lock:
        _active[req_id] = state

    def _run():
        try:
            target(state, *args)
        except BaseException:  # noqa: BLE001
            log.exception('worker crashed')
            tail = traceback.format_exc().strip().splitlines()[-1]
            _emit_error(req_id, ERR_INTERNAL, f'worker crashed: {tail}')
        finally:
            with _active_lock:
                _active.pop(req_id, None)

    thread = threading.Thread(target=_run, name=f'scyllasband-{req_id}', daemon=True)
    state.thread = thread
    thread.start()


# ---------------------------------------------------------------------------
# Handshake + frame loop
# ---------------------------------------------------------------------------
def _send_hello() -> None:
    _emit({
        'type': 'hello',
        'protocol': PROTOCOL_VERSION,
        'addon': ADDON_ID,
        'version': ADDON_VERSION,
        'capabilities': [{
            'task': 'tts.synthesize',
            'languages': list(voices.LANGUAGES),
            'voices': voices.manifest_voices(),
            'voice_clone': False,
        }],
    })


def _read_frame() -> dict | None:
    source = _PROTO_IN if _PROTO_IN is not None else sys.stdin.buffer
    line = source.readline()
    if not line:
        return None  # stdin closed: the host went away
    text = line.decode('utf-8', errors='replace').strip().lstrip('﻿')
    if not text:
        return {}
    try:
        frame = json.loads(text)
    except ValueError as exc:
        log.warning('malformed frame %r: %s', line[:200], exc)
        return {}
    return frame if isinstance(frame, dict) else {}


def _main_loop() -> int:
    while True:
        frame = _read_frame()
        if frame is None:
            log.info('stdin closed; exiting')
            return 0
        if not frame:
            continue
        ftype = frame.get('type')

        if ftype == 'shutdown':
            log.info('shutdown requested')
            return 0

        if ftype == 'ready':
            log.info('handshake complete; host=%s host_version=%s',
                     frame.get('host'), frame.get('host_version'))
            continue

        if ftype == 'cancel':
            target = frame.get('target')
            with _active_lock:
                state = _active.get(target) if isinstance(target, str) else None
            if state is not None:
                log.info('cancel %s', target)
                _cancel(state)
            continue

        if ftype == 'tts.synthesize':
            req_id = frame.get('id')
            params = frame.get('params')
            if not isinstance(req_id, str) or not req_id:
                log.warning('tts.synthesize without id')
                continue
            if params is None:
                params = {}
            if not isinstance(params, dict):
                _emit_error(req_id, ERR_BAD_PARAMS, 'params must be an object')
                continue
            _spawn(req_id, _handle_synthesize, params)
            continue

        # Unknown task: still answer, so the host's future resolves instead of
        # hanging until its timeout.
        req_id = frame.get('id')
        if isinstance(req_id, str) and req_id:
            _emit_error(req_id, ERR_BAD_PARAMS, f'unsupported frame type {ftype!r}')


def _stop_workers(budget_sec: float = 3.5) -> bool:
    """Cancel and join every worker. True when none is left running.

    The host allows 5 s between `shutdown` and SIGTERM, so stay under it.
    """
    with _active_lock:
        states = list(_active.values())
    for state in states:
        _cancel(state)
    deadline = time.monotonic() + budget_sec
    for state in states:
        if state.thread is not None:
            state.thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return not any(s.thread is not None and s.thread.is_alive() for s in states)


def _self_test() -> int:
    """Check what the frozen bundle needs, without the network or a model:
    the runtime and LiteRT load and report ABI 3, and the TLS and hashing
    modules the download uses are there."""
    import hashlib
    import ssl

    report: dict = {'version': ADDON_VERSION}
    try:
        report['runtime'] = engine.runtime_version()
        ok = True
    except engine.EngineError as exc:
        report['runtime'] = str(exc)
        ok = False
    report['voices'] = len(voices.VOICES)
    report['openssl'] = ssl.OPENSSL_VERSION
    report['sha256'] = hashlib.sha256(b'').hexdigest()[:8]
    report['ca_bundle'] = os.environ.get('SSL_CERT_FILE', '')
    report['ok'] = ok
    print(json.dumps(report), file=sys.__stdout__, flush=True)
    return 0 if ok else 1


def main() -> int:
    if '--version' in sys.argv[1:]:
        print(f'scyllasband-addon {ADDON_VERSION}')
        return 0
    logging.basicConfig(
        level=(_env('SCYLLASBAND_ADDON_LOG_LEVEL') or 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    tls.configure_ca_bundle()
    if '--self-test' in sys.argv[1:]:
        return _self_test()
    _claim_protocol_stdio()
    log.info('scyllasband-addon %s on %s %s, python %s',
             ADDON_VERSION, platform.system(), platform.machine(), platform.python_version())
    if not engine.library_path().is_file():
        # Not fatal: better to appear in the engine list and fail the first
        # request with a clear message than to vanish from the UI.
        log.warning("the Scylla's Band runtime is missing: %s", engine.library_path())

    _send_hello()
    rc = 1
    try:
        rc = _main_loop()
    except KeyboardInterrupt:
        rc = 130
    except Exception:  # noqa: BLE001
        log.exception('main loop crashed')
        rc = 1
    finally:
        # Never destroy the runtime under a line still running in it.
        runtime = engine.current()
        if _stop_workers() and runtime is not None:
            runtime.close()
    return rc


if __name__ == '__main__':
    sys.exit(main())
