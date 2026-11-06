"""The voices and languages of the pinned Scylla's Band release.

Copyright 2026 Subtitld contributors
SPDX-License-Identifier: Apache-2.0

Fourteen voices, each trained in all six languages: English (eleven voices in
American English, three in British English), Spanish, Italian, French, German
and Vietnamese. So every voice is listed once, for any of the add-on's
languages (`*`), and a line in English is spoken in the voice's own English.
The list is the `voices` of the bundle's manifest.json at the pinned revision;
tests/test_installed.py checks it against what the runtime reports.
"""

from __future__ import annotations

from dataclasses import dataclass

LANGUAGES: tuple[str, ...] = ('en', 'es', 'it', 'fr', 'de', 'vi')

LANGUAGE_NAMES = {
    'en': 'English', 'es': 'Spanish', 'it': 'Italian', 'fr': 'French', 'de': 'German', 'vi': 'Vietnamese',
}


@dataclass(frozen=True)
class Voice:
    id: str
    english: str  # the runtime's locale for the voice's English: en_us or en_gb

    @property
    def display_name(self) -> str:
        accent = 'British English' if self.english == 'en_gb' else 'American English'
        return f'{self.id.capitalize()} ({accent})'


_BRITISH = {'ink', 'orpheus', 'tuesday'}
VOICES: tuple[Voice, ...] = tuple(
    Voice(name, 'en_gb' if name in _BRITISH else 'en_us')
    for name in ('scylla', 'ariadne', 'felix', 'gwen', 'ink', 'kincade', 'max', 'orpheus',
                 'poppy', 'priya', 'rex', 'stone', 'thistle', 'tuesday'))
BY_ID: dict[str, Voice] = {v.id: v for v in VOICES}
DEFAULT_VOICE = 'scylla'
SAMPLE_RATE = 24000


def runtime_language(language: str, voice: Voice) -> str:
    """The runtime's language id for a line in `language` (one of LANGUAGES)."""
    return voice.english if language == 'en' else language


def manifest_voices() -> list[dict]:
    """The voice list as the handshake and manifest.json give it."""
    return [{'id': v.id, 'language': '*', 'display_name': v.display_name, 'sample_rate': SAMPLE_RATE}
            for v in VOICES]
