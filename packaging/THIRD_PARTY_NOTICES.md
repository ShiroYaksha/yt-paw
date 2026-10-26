# Third-party notices

yt-paw itself is distributed under the MIT license (see the project repository).

The standalone Windows and Linux builds bundle the following components:

| Component | Version | License | Source |
|-----------|---------|---------|--------|
| Python runtime | 3.13+ | PSF License | https://www.python.org/ |
| PySide6 / Qt | 6.11.2 | LGPL-3.0 (dynamically linked) | https://www.qt.io/ |
| FFmpeg (BtbN `lgpl-shared` build) | 8.1 | LGPL-3.0 | https://ffmpeg.org/ (DLLs on Windows, `lib/*.so*` on Linux) |
| Deno | 2.9.7 | MIT | https://github.com/denoland/deno |
| yt-dlp | 2026.08.19 | Unlicense | https://github.com/yt-dlp/yt-dlp |

License texts for the LGPL components are included in `licenses/`:

- `licenses/LGPL-3.0.txt` — applies to Qt/PySide6 and FFmpeg.
- `licenses/ffmpeg-LICENSE.txt` — FFmpeg's own notice, extracted from the
  upstream build archive.

Qt and FFmpeg are shipped as replaceable shared libraries on both platforms,
which satisfies the LGPL dynamic-linking requirement. Source code for the exact
Qt and FFmpeg versions is available from the upstream projects linked above.

Deno and yt-dlp are redistributed unmodified as standalone executables. Their
license texts are available at the project links above.
