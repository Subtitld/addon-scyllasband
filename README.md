# Scylla's Band add-on for Subtitld

Offline text-to-speech for [Subtitld](https://subtitld.org) with
[Scylla's Band](https://huggingface.co/spybyscript/scyllasband): fourteen
voices, each speaking **English, Spanish, Italian, French, German and
Vietnamese** at 24 kHz. Eleven voices speak American English and three
(Ink, Orpheus, Tuesday) British English.

- **Light and quick.** The model, about 240 MB, downloads on the first line.
  On a laptop CPU a line takes a small fraction of its own length, and the
  add-on uses about 500 MB of memory.
- **Delivery controls** in Configure, each from −2 to 2, with 0 for each
  voice's own delivery:
  - **Intensity**, from calm to intense;
  - **Pace**, from slower to faster;
  - **Pitch**, in semitones.
- **Built-in voices only.** Scylla's Band does not clone a speaker's voice.
- **Same seed, same take.** Subtitld gives each speaker a seed, so
  regenerating a line keeps its delivery.

## How it works

Synthesis is Scylla's Band's own C++ runtime, `libscyllasband`, with Google's
LiteRT running the graphs on the CPU. The add-on loads both in-process with
`ctypes`, so it needs neither numpy nor a Python machine-learning runtime.

The model is downloaded from `spybyscript/scyllasband` at a pinned revision
(release `v3-20261104`, `litert/` bundle). Every file is checked against its
sha256, and an interrupted download resumes on the next line. It lands in
Subtitld's model folder (`$SUBTITLD_MODELS_DIR/scyllasband/`).

## Licences

| Part | Licence |
|---|---|
| This add-on | Apache-2.0 |
| libscyllasband | Apache-2.0 |
| LiteRT | Apache-2.0 |
| The Scylla's Band weights | Apache-2.0 |

The weights include parts derived from Vocos and DeepPhonemizer, both MIT.
`THIRD_PARTY_LICENSES/` in each release archive covers everything bundled,
file by file.

The model card asks that the voices not be used to impersonate anyone or to
attribute speech to someone who did not say it.

## Building

```bash
python -m venv .venv && .venv/bin/pip install -e '.[build,dev]'
.venv/bin/python tools/build_native.py      # libscyllasband + LiteRT into build/assets/
.venv/bin/pytest                            # downloads the model on the first real line
.venv/bin/pyinstaller pyinstaller.spec --noconfirm
```

`tools/build_native.py` needs git, CMake and a C++17 compiler. On Windows it
needs the MSVC environment; on Linux it uses `patchelf` when it is installed.

The Linux release archive is built in an AlmaLinux 8 container, so it runs on
glibc 2.28 and newer (RHEL and AlmaLinux 8, Debian 10, Ubuntu 20.04 and
later). `tools/check_glibc.py` fails the build if any file in it needs more.

```bash
tools/build_linux.sh                    # scyllasband-<version>-linux-x86_64.zip; needs Docker
tools/test_archive.sh almalinux:8       # tests/test_installed.py on the zip, in a clean container
```

To move to another model release, run `tools/pin_model.py --tag <tag>`. The
tag must be a `v3-*` release: the pinned runtime speaks graph contract v3.
