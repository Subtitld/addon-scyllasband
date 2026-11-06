"""Speech through Scylla's Band's native runtime, loaded in-process with ctypes.

Copyright 2026 Subtitld contributors
SPDX-License-Identifier: Apache-2.0

libscyllasband (lowkeytea/scyllasband, Apache-2.0) is the C++ runtime of
Scylla's Band: text normalizer, G2P, the duration, pitch and flow graphs and
the vocoder, run by Google's LiteRT. Its C ABI (include/scyllasband.h, ABI
3.0.0) is small enough to bind here with ctypes, so the add-on needs neither
numpy nor a Python ML runtime. tools/build_native.py builds the library and
puts it, with libLiteRt, in build/assets/; the frozen add-on carries them in
_internal/assets/.

One runtime is created on the first line and kept: opening the graphs is most
of the first line's time. It serializes its requests, and the add-on sends one
line at a time anyway. `cancel()` stops the line in progress from the frame
loop's thread, at the runtime's next graph call.
"""

from __future__ import annotations

import array
import ctypes
import json
import logging
import os
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger('scyllasband_addon.engine')

ABI_MAJOR = '3.'

_PACKAGE = Path(__file__).resolve().parent


class EngineError(Exception):
    """The runtime failed, or could not be loaded."""


class Cancelled(Exception):
    """The line was cancelled."""


def pins() -> dict:
    return json.loads((_PACKAGE / 'pins.json').read_text(encoding='utf-8'))


def assets_dir() -> Path:
    """Where the native libraries are: the frozen bundle's, $SCYLLASBAND_ASSETS, or build/assets/."""
    override = (os.environ.get('SCYLLASBAND_ASSETS') or '').strip()
    if override:
        return Path(override)
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)) / 'assets'
    return _PACKAGE.parent / 'build' / 'assets'


def library_names() -> tuple[str, str]:
    """(LiteRT, libscyllasband) file names on this platform."""
    if sys.platform == 'win32':
        return 'libLiteRt.dll', 'scyllasband_native.dll'
    if sys.platform == 'darwin':
        return 'libLiteRt.dylib', 'libscyllasband_native.dylib'
    return 'libLiteRt.so', 'libscyllasband_native.so'


def library_path() -> Path:
    return assets_dir() / library_names()[1]


def bundle_dir(models: Path | None = None) -> Path:
    """The downloaded bundle: the folder holding its manifest.json."""
    from scyllasband_addon import download
    data = pins()
    root = models if models is not None else download.cache_root() / data['revision']
    return root / data['bundle']


# ---------------------------------------------------------------------------
# The C ABI
# ---------------------------------------------------------------------------
OK, INVALID_ARGUMENT, RUNTIME_ERROR, CANCELLED = 0, 1, 2, 3
EVENT_PLAN, EVENT_CHUNK_STARTED, EVENT_AUDIO, EVENT_DONE = 1, 2, 3, 4
ACCELERATOR_CPU = 0


class _RuntimeOptions(ctypes.Structure):
    _fields_ = [('bundle_dir', ctypes.c_char_p),
                ('threads', ctypes.c_int32),
                ('accelerator', ctypes.c_int)]


class _Request(ctypes.Structure):
    _fields_ = [('text', ctypes.c_char_p),
                ('voice_id', ctypes.c_char_p),
                ('language', ctypes.c_char_p),
                ('delivery', ctypes.c_char_p),
                ('speed', ctypes.c_float),
                ('steps', ctypes.c_int32),
                ('sampler', ctypes.c_int),
                ('seed', ctypes.c_uint64),
                ('has_seed', ctypes.c_int32),
                ('temperature', ctypes.c_float),
                ('normalize_text', ctypes.c_int32)]


class _Event(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int),
                ('chunk_index', ctypes.c_int32),
                ('chunk_count', ctypes.c_int32),
                ('chunk_id', ctypes.c_char_p),
                ('metadata_json', ctypes.c_char_p),
                ('samples', ctypes.POINTER(ctypes.c_float)),
                ('sample_count', ctypes.c_int64),
                ('sample_rate', ctypes.c_int32)]


_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.POINTER(_Event), ctypes.c_void_p)

_lib = None
_lib_lock = threading.Lock()


def load_library():
    """Load libLiteRt and libscyllasband from assets_dir(), once."""
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        folder = assets_dir()
        litert_name, native_name = library_names()
        native = folder / native_name
        if not native.is_file():
            raise EngineError(f'The Scylla\'s Band runtime is missing from the add-on: {native}')
        try:
            if sys.platform == 'win32':
                os.add_dll_directory(str(folder))
            # LiteRT first, globally: libscyllasband then finds it already
            # loaded, whatever its own search path says.
            ctypes.CDLL(str(folder / litert_name), mode=getattr(ctypes, 'RTLD_GLOBAL', 0))
            lib = ctypes.CDLL(str(native))
        except OSError as exc:
            raise EngineError(f'Could not load the Scylla\'s Band runtime: {exc}') from exc

        lib.scyllasband_version.restype = ctypes.c_char_p
        lib.scyllasband_backend.restype = ctypes.c_char_p
        lib.scyllasband_last_error.restype = ctypes.c_char_p
        lib.scyllasband_request_init.argtypes = [ctypes.POINTER(_Request)]
        lib.scyllasband_request_init.restype = None
        lib.scyllasband_runtime_create.argtypes = [ctypes.POINTER(_RuntimeOptions), ctypes.POINTER(ctypes.c_void_p)]
        lib.scyllasband_runtime_create.restype = ctypes.c_int
        lib.scyllasband_runtime_destroy.argtypes = [ctypes.c_void_p]
        lib.scyllasband_runtime_destroy.restype = None
        lib.scyllasband_voices_json.argtypes = [ctypes.c_void_p]
        lib.scyllasband_voices_json.restype = ctypes.c_char_p
        lib.scyllasband_synthesize_stream.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Request), _CALLBACK,
                                                      ctypes.c_void_p]
        lib.scyllasband_synthesize_stream.restype = ctypes.c_int
        lib.scyllasband_cancel.argtypes = [ctypes.c_void_p]
        lib.scyllasband_cancel.restype = None

        version = lib.scyllasband_version().decode()
        if not version.startswith(ABI_MAJOR):
            raise EngineError(f'libscyllasband {version} is not ABI {ABI_MAJOR}x')
        _lib = lib
        return lib


def runtime_version() -> str:
    lib = load_library()
    return f'libscyllasband {lib.scyllasband_version().decode()} ({lib.scyllasband_backend().decode()})'


def _last_error(lib) -> str:
    message = lib.scyllasband_last_error()
    return message.decode('utf-8', 'replace') if message else 'unknown error'


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------
@dataclass
class Speech:
    samples: array.array  # mono float32 in [-1, 1]
    sample_rate: int
    seconds: float        # synthesis wall time

    @property
    def duration_sec(self) -> float:
        return len(self.samples) / self.sample_rate


class Runtime:
    """One loaded bundle."""

    def __init__(self, bundle: Path, threads: int = 0):
        self._lib = load_library()
        manifest = bundle / 'manifest.json'
        if not manifest.is_file():
            raise EngineError(f'The model is incomplete: {manifest} is missing')
        self.bundle = bundle
        self.threads = threads
        options = _RuntimeOptions(str(bundle).encode('utf-8'), threads, ACCELERATOR_CPU)
        handle = ctypes.c_void_p()
        started = time.monotonic()
        if self._lib.scyllasband_runtime_create(ctypes.byref(options), ctypes.byref(handle)) != OK:
            raise EngineError(f'Could not open the model: {_last_error(self._lib)}')
        self._handle = handle
        log.info('opened %s (%s threads) in %.2fs', bundle, threads or 'default', time.monotonic() - started)

    def voices(self) -> dict:
        return json.loads(self._lib.scyllasband_voices_json(self._handle).decode('utf-8'))

    def cancel(self) -> None:
        if self._handle:
            self._lib.scyllasband_cancel(self._handle)

    def close(self) -> None:
        if self._handle:
            self._lib.scyllasband_runtime_destroy(self._handle)
            self._handle = None

    def synthesize(self, text: str, voice: str, language: str, delivery: str, seed: int | None,
                   progress: Callable[[float], None] = lambda fraction: None,
                   cancel: threading.Event | None = None) -> Speech:
        request = _Request()
        self._lib.scyllasband_request_init(ctypes.byref(request))
        # Kept referenced until the call returns.
        fields = [s.encode('utf-8') for s in (text, voice, language, delivery)]
        request.text, request.voice_id, request.language, request.delivery = fields
        if seed is not None:
            request.seed = seed
            request.has_seed = 1
        samples = array.array('f')
        state = {'rate': 0, 'error': None}

        def on_event(event_ptr, _user) -> int:
            try:
                event = event_ptr.contents
                if cancel is not None and cancel.is_set():
                    return 1
                if event.type == EVENT_AUDIO and event.sample_count > 0:
                    state['rate'] = event.sample_rate
                    samples.frombytes(ctypes.string_at(event.samples, event.sample_count * 4))
                elif event.type == EVENT_CHUNK_STARTED and event.chunk_count > 0:
                    progress(event.chunk_index / event.chunk_count)
                return 0
            except Exception as exc:  # noqa: BLE001 — never let an exception cross into C++
                state['error'] = exc
                return 1

        callback = _CALLBACK(on_event)
        started = time.monotonic()
        status = self._lib.scyllasband_synthesize_stream(self._handle, ctypes.byref(request), callback, None)
        seconds = time.monotonic() - started
        if state['error'] is not None:
            raise EngineError(f'{type(state["error"]).__name__}: {state["error"]}')
        if status == CANCELLED or (cancel is not None and cancel.is_set()):
            raise Cancelled()
        if status != OK:
            raise EngineError(_last_error(self._lib))
        if not samples:
            raise EngineError('the runtime returned no audio')
        return Speech(samples, state['rate'], seconds)


_runtime: Runtime | None = None


def runtime(bundle: Path, threads: int) -> Runtime:
    """The kept runtime, reopened when the bundle or the thread count changed."""
    global _runtime
    if _runtime is not None and (_runtime.bundle != bundle or _runtime.threads != threads):
        _runtime.close()
        _runtime = None
    if _runtime is None:
        _runtime = Runtime(bundle, threads)
    return _runtime


def current() -> Runtime | None:
    return _runtime


def write_wav(path: Path, speech: Speech) -> None:
    """16-bit mono WAV, the format Subtitld's other voice engines hand it."""
    # Clipped to [-1, 1]; a NaN (which no comparison holds for) becomes silence.
    pcm = array.array('h', (int(s * 32767.0) if -1.0 < s < 1.0 else
                            32767 if s >= 1.0 else -32767 if s <= -1.0 else 0
                            for s in speech.samples))
    if sys.byteorder != 'little':
        pcm.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + '.part')
    with wave.open(str(part), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(speech.sample_rate)
        out.writeframes(pcm.tobytes())
    os.replace(part, path)
