# LiteRT runtime

`libLiteRt` (`.so` on Linux, `.dylib` on macOS, `.dll` on Windows) is Google's
LiteRT 2.2.0, the prebuilt runtime published at
https://storage.googleapis.com/litert/binaries/2.2.0/, redistributed unmodified.
LiteRT is Apache-2.0 (`LICENSE`); its source is https://github.com/google-ai-edge/LiteRT.

The library contains, compiled in, the projects below. Their licences:

| Project | Licence | Text here |
|---|---|---|
| LiteRT | Apache-2.0 | `LICENSE` |
| TensorFlow Lite (LiteRT's predecessor, from the TensorFlow repository) | Apache-2.0 | `tensorflow-LICENSE` |
| XNNPACK | BSD-3-Clause | `XNNPACK-LICENSE` |
| cpuinfo | BSD-2-Clause | `cpuinfo-LICENSE` |
| pthreadpool | BSD-2-Clause | `pthreadpool-LICENSE` |
| FP16 | MIT | `FP16-LICENSE` |
| FXdiv | MIT | `FXdiv-LICENSE` |
| Abseil, ruy, gemmlowp, FlatBuffers, OpenCL headers | Apache-2.0 | `abseil-LICENSE` (the Apache-2.0 text) |
| Protocol Buffers | BSD-3-Clause | `protobuf-LICENSE` |
| Eigen | MPL-2.0 | `eigen-COPYING.MPL2` |
| Ooura FFT (fft2d) | Ooura's notice | `fft2d-NOTICE.txt` |

Eigen is MPL-2.0. Its source, unmodified by LiteRT, is available from
https://gitlab.com/libeigen/eigen; the revision is the one LiteRT 2.2.0's
build files pin.

The texts were fetched from each project's upstream repository. The list is
based on the symbols present in the released library.
