# Parsec Android Video Freeze Fix

Binary patch for Parsec Android (`tv.parsec.client` `3.150.097.05`, `arm64-v8a`) that fixes the video-only freeze where the stream works for a few seconds and then stops updating while keyboard/text input continues to work.

## Root Cause

In Parsec's native `libmain.so` (`libmatoya` / `MTY` `AMediaCodec` wrapper):

1. `AMediaCodec_dequeueInputBuffer(codec, -1)` is called with an **infinite timeout (`-1`)**.
2. `AMediaCodec_dequeueOutputBuffer(codec, &info, 0)` is called **once** per input frame with `0` timeout and returns immediately on `INFO_OUTPUT_FORMAT_CHANGED` (`-2`) or `INFO_OUTPUT_BUFFERS_CHANGED` (`-3`) without draining pending output buffers.

When Codec2 output buffers back up in `AidlBufferPool` (e.g. all 6 buffers stuck as `used` at 4K `3840x2160` after format changes or brief decode latency spikes), the decoder stops accepting input and `AMediaCodec_dequeueInputBuffer(codec, -1)` deadlocks the video decode thread forever while input/network threads keep running.

## What the Patch Does

`patch_parsec.py` adds a 16 KB-aligned `r-x` `PT_LOAD` code cave (repurposing `PT_NOTE`) to `libmain.so`:

- Replaces `AMediaCodec_dequeueInputBuffer(codec, -1)` (`0x16b888`) with a bounded 5 ms poll (up to 40 retries = 200 ms) that drains and delivers backed-up output frames via `decode_frame(ctx, NULL, 0, out_cb, out_status)` whenever `INFO_TRY_AGAIN_LATER` (`-1`) occurs.
- Loops `AMediaCodec_dequeueOutputBuffer` (`0x16b904` -> `0x16b8e8`, reloading `codec` into `x0`) on `INFO_OUTPUT_FORMAT_CHANGED` (`-2`) and `INFO_OUTPUT_BUFFERS_CHANGED` (`-3`) instead of skipping the frame.
- (Optional, when `apktool` + Pillow are installed) Updates `base.apk`'s launcher icon to modern Android adaptive + monochrome specs (`mipmap-anydpi-v26/ic_launcher.xml` and `ic_launcher_round.xml` with 432×432 foreground/background/monochrome layers), removing the legacy white border on Pixel Launcher and adding Android 13+ themed/grayscale icon support.

## Usage

Make sure `adb`, `python3`, `zipalign`, `apksigner`, and `keytool` are on `PATH`, then run:

```bash
chmod +x patch_apk.sh
./patch_apk.sh
```

> **Note:** Re-signing changes the APK signature, so `patch_apk.sh` uninstalls the Play Store package first (you will need to sign back into Parsec).
