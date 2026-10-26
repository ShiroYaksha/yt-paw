# Packaging — standalone builds

yt-paw ships as a **PyInstaller one-folder** bundle. The folder carries Python,
Qt/PySide6, yt-dlp, FFmpeg, and Deno, so end users do not install Python or the
engines themselves.

| Host | Target | Archive |
|------|--------|---------|
| Windows x86_64 | Windows x86_64 | `.zip` |
| Linux x86_64 | Linux x86_64 | `.tar.gz` |

> PyInstaller cannot cross-compile. Build the Windows artifact on Windows and
> the Linux artifact on Linux (`windows-latest` / `ubuntu-latest` in CI). The
> build refuses to run when the host has no pinned sidecars; use
> `--no-sidecars` for a layout-only development build.

## Quick start

Windows (PowerShell):

```powershell
uv run --group build python packaging/build.py
```

Linux:

```bash
uv run --group build python packaging/build.py
```

Install the build toolchain first if the `build` group is not synced yet:

```bash
uv sync --group build
```

## Output layout

Windows:

```
dist/yt-paw/
  yt-paw.exe
  bin/                       # yt-dlp.exe, deno.exe, ffmpeg.exe, ffprobe.exe, av*/sw*.dll
  _internal/                 # Python, Qt, app resources
  lib/                       # (absent on Windows)
  licenses/
  THIRD_PARTY_NOTICES.md
dist/yt-paw-<version>-windows-x86_64.zip
dist/yt-paw-<version>-windows-x86_64.zip.sha256
```

Linux:

```
dist/yt-paw/
  yt-paw                     # executable
  bin/                       # yt-dlp, deno, ffmpeg, ffprobe
  lib/                       # libav*.so*, libsw*.so* (FFmpeg shared libraries)
  _internal/                 # Python, Qt, app resources
  licenses/
  THIRD_PARTY_NOTICES.md
dist/yt-paw-<version>-linux-x86_64.tar.gz
dist/yt-paw-<version>-linux-x86_64.tar.gz.sha256
```

Unpack and run. The Windows build needs no installer or admin rights and is
unsigned, so SmartScreen may warn until a signing certificate is configured.
On Linux, extract with `tar -xzf …` so executable bits and FFmpeg's versioned
symlinks (`libavcodec.so.62 -> libavcodec.so.62.28.103`) are preserved:

```bash
tar -xzf yt-paw-<version>-linux-x86_64.tar.gz
./yt-paw/yt-paw
```

## Linux runtime requirements

Python, Qt, and the engines are bundled, but the Qt `xcb` platform plugin still
uses the operating system's X11/OpenGL stack. Install the usual desktop
libraries before the first run. Debian/Ubuntu:

```bash
sudo apt install libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
  libxcb-shape0 libxcb-xinerama0 libxcb-xfixes0 libxcb-randr0 \
  libxcb-render0 libxcb-shm0 libxcb-sync1 libxcb-glx0 libxcb-xkb1 \
  libxkbcommon0 fontconfig
```

Fedora:

```bash
sudo dnf install mesa-libGL libxkbcommon-x11 xcb-util-cursor xcb-util-wm \
  xcb-util-image xcb-util-keysyms xcb-util-renderutil xcb-util xcb-util-xrm \
  fontconfig
```

Arch:

```bash
sudo pacman -S mesa libxkbcommon-x11 xcb-util-cursor xcb-util-wm \
  xcb-util-image xcb-util-keysyms xcb-util-renderutil xcb-util fontconfig
```

These are inherited from the distro on purpose: shipping glibc/X11/GL would
tie the bundle to one distribution. Build on the **oldest glibc** you intend to
support (for example Ubuntu 22.04) — a binary built against newer glibc will
not start on older systems.

FFmpeg is the exception: its shared libraries are bundled in `lib/` and the
binaries in `bin/` carry `RPATH=$ORIGIN/../lib`, so no `LD_LIBRARY_PATH` is
needed. yt-dlp finds `ffmpeg`/`ffprobe` through the `bin/` directory that
`infra/runtime_paths.py` prepends to child-process `PATH`.

## What is bundled

| Piece | Source | Notes |
|-------|--------|-------|
| Python runtime | build interpreter (3.13 in CI) | frozen by PyInstaller |
| PySide6 / Qt | `pyproject.toml` | exact pin; only the Qt modules the app imports |
| yt-dlp | `packaging/sidecars.lock.json` | Unlicense; auto-updates at runtime |
| Deno | `sidecars.lock.json` | MIT; used by yt-dlp for JS challenge solving |
| FFmpeg | `sidecars.lock.json` (BtbN `lgpl-shared`) | LGPL shared build + DLLs on Windows, `.so` libraries on Linux |

The sidecar lockfile pins an exact URL and SHA-256 for every download, per
platform. A checksum mismatch aborts the build instead of shipping unreviewed
bytes.

## Dependency updates

Pinned Python and native dependencies use a deliberately slow release train:
quarterly-ish, with a minimum release age. Do not bump a version merely because
upstream published one. The single entry point is `packaging/deps.py`; the full
policy and agent runbook are in [`dependency-updates.md`](dependency-updates.md).

```bash
uv run python packaging/deps.py status          # read-only check
uv run python packaging/deps.py update --due    # only when the train is due
uv run python packaging/deps.py verify
```

`packaging/dependency-policy.json` controls cadence; `packaging/dependency-state.json`
records the last review. Both must be committed with a dependency change.
`uv.lock` must stay tracked as well.

`fetch_sidecars.py --update-pins` remains a low-level checksum refresh for
debugging and for FFmpeg's rolling build. Routine updates should go through
`deps.py update --due`, which also handles Python pins, versions, and state.

## Build options

```bash
uv run --group build python packaging/build.py --no-sidecars      # dev build, no yt-dlp/ffmpeg/deno
uv run --group build python packaging/build.py --force-sidecars   # ignore build/sidecar-cache/
uv run --group build python packaging/build.py --skip-self-check  # skip the frozen-app smoke test
uv run --group build python packaging/build.py --skip-verify      # skip layout verification
uv run --group build python packaging/build.py --no-zip           # keep only dist/yt-paw/
uv run --group build python packaging/build.py --keep             # reuse build/ and dist/yt-paw/
```

`--no-zip` means "skip the archive" on both platforms.

Sidecars can be fetched separately, for the host or another platform:

```bash
uv run python packaging/fetch_sidecars.py                     # host platform
uv run python packaging/fetch_sidecars.py --platform linux-x86_64
uv run python packaging/fetch_sidecars.py --force             # bypass the download cache
uv run python packaging/fetch_sidecars.py --only ffmpeg
uv run python packaging/fetch_sidecars.py --update-pins       # refresh this platform's pins
uv run python packaging/fetch_sidecars.py --platform all --update-pins
```

`--update-pins` fetches each upstream checksum file and rewrites the `sha256`
fields in `packaging/sidecars.lock.json`. It is a low-level repair/debug tool,
not the routine update path; use `deps.py update --due` for releases. Review any
pin diff before committing. FFmpeg's BtbN URLs are rolling `latest` assets, so
their pins must be refreshed when upstream publishes a new build; builds fail
closed until then.

## Runtime layout and updates

Windows:

```
<install>/bin/
  yt-dlp.exe          # bundled stable fallback
  ffmpeg.exe          # + ffprobe.exe and av*/sw* DLLs
  deno.exe
```

Linux:

```
<install>/bin/
  yt-dlp
  ffmpeg              # + ffprobe; libs in ../lib
  deno
<install>/lib/
  libavcodec.so*      # FFmpeg shared libraries + versioned symlinks
  libavformat.so*
  libavutil.so*
  ...
```

`infra/runtime_paths.py` puts app-managed and bundled `bin/` directories on the
`PATH` of every engine child process. For engines with `source=auto` (the fresh
install default) and for dependency lookup, resolution order is:

1. `<app-data>/bin/` — downloaded/auto-updated by the app
2. `<install>/bin/` — what the build ships
3. system `PATH` — respected but never overwritten

`source=path` uses the user-provided binary; `source=system` uses system `PATH`
only and is never written by the updater.

yt-dlp auto-updates into `<app-data>/bin/`, not the install folder. This keeps
Program Files (or any read-only location) untouched and needs no elevation. The
bundled copy remains the offline/first-run fallback.

App data stays in `~/.config/yt-paw/` (`app.db`, `logs/`, managed binaries).
Set `YTPAW_PROFILE_DIR` to use a different profile (also used by the build's
self-check).

## Verification

`uv run --group build python packaging/build.py` runs two checks automatically
on both platforms:

1. `packaging/verify_build.py` checks the executable, packaged QSS/icons, Qt
   platform/TLS/JPEG plugins, and the platform's sidecars (`.exe` + FFmpeg DLLs
   on Windows; executables + `lib/libav*.so*` on Linux).
2. `yt-paw[.exe] --self-check` starts the frozen app against a scratch profile,
   resolves every engine/dependency, probes their versions, checks UI assets,
   writes `<profile>/self-check.txt`, and exits non-zero on failure. Because it
   imports Qt at startup, it also catches missing Qt libraries.

The same check can be run manually.

Windows:

```powershell
$env:YTPAW_PROFILE_DIR = "$env:TEMP\yt-paw-check"
.\dist\yt-paw\yt-paw.exe --self-check
Get-Content "$env:TEMP\yt-paw-check\self-check.txt"
```

Linux:

```bash
YTPAW_PROFILE_DIR=/tmp/yt-paw-check ./dist/yt-paw/yt-paw --self-check
cat /tmp/yt-paw-check/self-check.txt
```

The report lists each engine/dependency with `origin=bundled` when the packaged
sidecars are being used.

## Releasing

A release is tag-driven. The tag must match the version in
`src/ytpaw/__init__.py` and `pyproject.toml` (`v0.1.1` for version `0.1.1`).

### What is a remote?

A Git remote is a named URL for another copy of the repository, usually on
GitHub. `origin` is the conventional name for the primary remote, but a fork or
mirror can have any name. List the configured remotes with:

```bash
git remote -v
```

The examples below use `origin`. Replace it with the remote that points at the
GitHub repository where the release workflow should run. Pushing a tag to a
different remote does not trigger this repository's workflow.

### Human release checklist

1. Start from an up-to-date default branch:

   ```bash
   git switch master
   git pull --ff-only origin master
   ```

2. Make and test the change.

3. Bump the version in both files:

   ```text
   src/ytpaw/__init__.py   __version__ = "0.1.2"
   pyproject.toml          version = "0.1.2"
   ```

4. Stage the intended files, commit, and push:

   ```bash
   git add <files>
   git commit -m "release: 0.1.2"
   git push origin master
   ```

5. Optional: validate both builds before tagging with a manual workflow run:

   ```bash
   gh workflow run build.yml --ref master
   gh run list --workflow=build.yml
   ```

   Or use **Actions → build → Run workflow** in GitHub. A manual run uploads
   temporary artifacts but does not create or publish a release.

6. Create and push the tag. This is what starts the release workflow:

   ```bash
   VERSION=0.1.2
   git tag -a "v$VERSION" -m "yt-paw $VERSION"
   git push origin "v$VERSION"
   ```

7. Verify the release after the workflow finishes:

   ```bash
   gh release view "v$VERSION"
   ```

   Or open **Releases** on GitHub and confirm both archives and both checksum
   files are attached.

The workflow rejects a tag that does not match both version fields. Normally,
do not create the GitHub Release manually; the tag push creates it and
publishes it. The fallback below is for when that job is skipped or fails.

#### Re-tagging after a failed run

Delete any existing GitHub Release for the tag first, then move the tag to the
fixed commit and push it again:

```bash
VERSION=0.1.2
git tag -d "v$VERSION"
git push origin --delete "v$VERSION"
git tag -a "v$VERSION" -m "yt-paw $VERSION"
git push origin "v$VERSION"
```

### Automatic release

`.github/workflows/build.yml` then:

1. validates the tag/version match;
2. runs lint, type checks, and tests on Linux;
3. builds both artifacts in parallel (Windows on `windows-latest`, Linux on
   `ubuntu-22.04`);
4. creates a **draft** GitHub Release with generated release notes and uploads
   both archives and their `.sha256` files;
5. publishes the release only after all assets are attached.

CI installs the Qt runtime libraries listed above and builds Linux on Ubuntu
22.04 for glibc compatibility; `ubuntu-latest` is not used for release builds.

If an earlier build step fails, no release is created. If the release job
starts but fails while uploading, the release remains a draft. Fix the problem
and move the tag as described above. Manual `workflow_dispatch` runs only
produce temporary workflow artifacts; they do not publish a release.

### Manual release from Actions artifacts

Use this fallback when the automatic release job was skipped or failed, or when
you deliberately want to create the release page yourself.

GitHub's UI cannot attach an Actions artifact directly to a release. Download
and extract the artifact wrappers first.

1. Open **Actions → build → the run for the tag** and wait for `quality`,
   `windows`, and `linux` to pass.
2. Under **Artifacts**, download:
   - `yt-paw-windows-x86_64`
   - `yt-paw-linux-x86_64`
3. Extract each downloaded wrapper ZIP. It contains the actual release files:

   ```text
   yt-paw-0.1.1-windows-x86_64.zip
   yt-paw-0.1.1-windows-x86_64.zip.sha256
   yt-paw-0.1.1-linux-x86_64.tar.gz
   yt-paw-0.1.1-linux-x86_64.tar.gz.sha256
   ```

4. Open **Releases → Draft a new release**.
5. Select the **existing** tag (`v0.1.1`). Do not create a second tag.
6. Add the title and release notes.
7. Drag the four extracted files into **Attach binaries**.
8. Publish the release.

If the automatic release job already created the release, edit that release
instead of creating another one. If an asset is missing, use **Edit release**
and attach it there.

Do not upload the `yt-paw-windows-x86_64` / `yt-paw-linux-x86_64` wrapper ZIPs,
and do not upload GitHub's generated source-code archives. The wrapper ZIPs are
Actions containers, not user-facing release assets. Actions artifacts expire;
release assets do not.

## Licensing

The bundle includes LGPL components, so `packaging/THIRD_PARTY_NOTICES.md` and
`licenses/LGPL-3.0.txt` are copied into every distribution. Qt and FFmpeg are
shipped as replaceable shared libraries on both platforms (Windows DLLs, Linux
`.so` files in `lib/`), not statically linked. If you add another sidecar or
engine, update the notices and the lockfile together.

## Size notes

A full bundle is large because it contains Qt, Deno, FFmpeg and yt-dlp
(roughly 350–450 MB unpacked on Windows and 450–550 MB on Linux; ~200–300 MB
compressed). Use `--no-sidecars` for layout/dev builds. Drop Deno only if you
accept slower/limited YouTube extraction; the app will download it on first run
if missing.
