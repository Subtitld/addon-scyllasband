#!/usr/bin/env python3
"""Pin the Scylla's Band model files the add-on downloads: scyllasband_addon/pins.json.

Copyright 2026 Subtitld contributors
SPDX-License-Identifier: Apache-2.0

Resolves a release tag of spybyscript/scyllasband on Hugging Face to its
commit, lists the `litert/` bundle at that commit, and takes each file's size
and sha256 from the release's own SHA256SUMS.json. The model's LICENSE and
THIRD_PARTY_NOTICES.md are pinned too, so the downloaded folder carries them.

    python tools/pin_model.py                 # the tag below
    python tools/pin_model.py --tag v3-20261104

The release has to match the graph contract of the runtime tools/build_native.py
builds (`scyllasband_measured_controls_v3` for ABI 3.0.0): a v4 bundle needs
the v4 runtime.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

REPO = 'spybyscript/scyllasband'
TAG = 'v3-20261104'
FLAVOR = 'litert'
EXTRA = ('LICENSE', 'THIRD_PARTY_NOTICES.md')
OUT = Path(__file__).resolve().parents[1] / 'scyllasband_addon' / 'pins.json'
HF = 'https://huggingface.co'


def _get(url: str):
    request = urllib.request.Request(url, headers={'User-Agent': 'subtitld-scyllasband-pin/1'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def pin(tag: str) -> dict:
    refs = _get(f'{HF}/api/models/{REPO}/refs')
    revision = next((t['targetCommit'] for t in refs['tags'] if t['name'] == tag), None)
    if revision is None:
        raise SystemExit(f'no tag {tag!r} in {REPO}')
    sums = _get(f'{HF}/{REPO}/resolve/{revision}/SHA256SUMS.json')
    tree = _get(f'{HF}/api/models/{REPO}/tree/{revision}/{FLAVOR}?recursive=1')
    paths = sorted(entry['path'] for entry in tree if entry['type'] == 'file') + list(EXTRA)
    files = []
    for path in paths:
        if path not in sums:
            raise SystemExit(f'{path} is not in the release SHA256SUMS.json')
        files.append({'path': path, 'size': sums[path]['bytes'], 'sha256': sums[path]['sha256']})
    sizes = {entry['path']: entry['size'] for entry in tree if entry['type'] == 'file'}
    for entry in files:
        if entry['path'] in sizes and sizes[entry['path']] != entry['size']:
            raise SystemExit(f'{entry["path"]}: tree says {sizes[entry["path"]]} bytes, SHA256SUMS {entry["size"]}')
    return {'repo': REPO, 'tag': tag, 'revision': revision, 'bundle': FLAVOR, 'files': files}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tag', default=TAG)
    args = parser.parse_args(argv)
    data = pin(args.tag)
    OUT.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    total = sum(f['size'] for f in data['files'])
    print(f'{OUT}: {len(data["files"])} files, {total / 1e6:.1f} MB at {data["revision"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
