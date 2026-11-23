# Parsec Android Video Freeze Fix

[![Build & Release Android APK](https://github.com/JEF1056/parsec-android-freeze-fix/actions/workflows/release.yml/badge.svg)](https://github.com/JEF1056/parsec-android-freeze-fix/actions/workflows/release.yml)
[![GitHub Release](https://img.shields.io/github/v/release/JEF1056/parsec-android-freeze-fix?include_prereleases&label=Latest%20Release)](https://github.com/JEF1056/parsec-android-freeze-fix/releases/latest)

Binary patch for Parsec Android (`tv.parsec.client` `3.150.097.05`, `arm64-v8a`) that fixes the video-only freeze where the stream works for a few seconds and then stops updating while keyboard/text input continues to work.

---

## 🚀 Quick Install (Ready-to-Install APK)

Pre-built, production-optimized APKs are built automatically via CI/CD on every push to this repo.

1. **Download:** Go to [**Releases**](https://github.com/JEF1056/parsec-android-freeze-fix/releases/latest) and download `parsec-3.150.097.05-arm64-freeze-fix.apk`.
2. **Uninstall Stock:** Uninstall the existing Play Store version from your device (required because this APK uses a release signature).
3. **Install:** Tap the downloaded APK to install. It is a single, standalone APK (no split APK installer or `adb install-multiple` required).
4. Launch Parsec, sign in, and stream with zero freeze!

---

## Root Cause

In Parsec's native `libmain.so` (`libmatoya` / `MTY` `AMediaCodec` wrapper):

1. `AMediaCodec_dequeueInputBuffer(codec, -1)` is called with an **infinite timeout (`-1`)**.
2. `AMediaCodec_dequeueOutputBuffer(codec, &info, 0)` is called **once** per input frame with `0` timeout and returns immediately on `INFO_OUTPUT_FORMAT_CHANGED` (`-2`) or `INFO_OUTPUT_BUFFERS_CHANGED` (`-3`) without draining pending output buffers.

When Codec2 output buffers back up in `AidlBufferPool` (e.g. all 6 buffers stuck as `used` at 4K `3840x2160` after format changes or brief decode latency spikes), the decoder stops accepting input and `AMediaCodec_dequeueInputBuffer(codec, -1)` deadlocks the video decode thread forever while input/network threads keep running.

---

## What the Patch & Build Pipeline Does

- **Deadlock Fix (`patch_parsec.py`):** Adds a 16 KB-aligned `r-x` `PT_LOAD` code cave (repurposing `PT_NOTE`) to `libmain.so`:
  - Replaces `AMediaCodec_dequeueInputBuffer(codec, -1)` (`0x16b888`) with a bounded 5 ms poll (up to 40 retries = 200 ms) that drains and delivers backed-up output frames via `decode_frame(ctx, NULL, 0, out_cb, out_status)` whenever `INFO_TRY_AGAIN_LATER` (`-1`) occurs.
  - Loops `AMediaCodec_dequeueOutputBuffer` (`0x16b904` -> `0x16b8e8`, reloading `codec` into `x0`) on `INFO_OUTPUT_FORMAT_CHANGED` (`-2`) and `INFO_OUTPUT_BUFFERS_CHANGED` (`-3`) instead of skipping the frame.
- **Standalone 1-Tap APK:** Merges `libmain.so` directly into `base.apk` and removes split APK metadata from `AndroidManifest.xml` so the resulting APK installs immediately with a single tap.
- **Android 15+ 16 KB Page Alignment:** All uncompressed `.so` libraries (`extractNativeLibs="false"`) and the code cave are aligned to 16 KB page boundaries (`zipalign -P 16 -f 4`) for compatibility with Android 15 devices (Pixel 8, 9, etc.) and zero-copy `mmap`.
- **Modern Adaptive & Themed Icons:** Generates 432×432 adaptive foreground/background layers and Android 13+ monochrome icon definitions, removing the legacy white border on modern launchers.
- **Production Signing:** Signed using `apksigner` with APK Signature Schemes v1, v2, and v3.
- **Automated CI/CD:** GitHub Actions workflow builds, verifies, and publishes GitHub releases with SHA256 checksums on every repository push.

---

## Building Locally

### Prerequisites
- Python 3.9+ with `Pillow` (`pip install Pillow`)
- `apktool` (`brew install apktool` or downloaded from GitHub)
- Android SDK build-tools (`zipalign`, `apksigner`)
- Java / `keytool`

### Run Build
```bash
python3 build.py
```

The script will automatically download the stock Parsec APK (or use an existing file in `orig/` or `--xapk`), patch it, align it, and generate the signed APKs in `./dist/`:
- `dist/parsec-3.150.097.05-arm64-freeze-fix.apk` (Standalone 1-tap install APK)
- `dist/parsec-3.150.097.05-arm64-splits.zip` (Split APKs bundle)
- `dist/libmain_patched.so` (Patched native library)

### Sideloading via ADB
To install directly to a connected Android device:
```bash
adb install -r dist/parsec-3.150.097.05-arm64-freeze-fix.apk
```
