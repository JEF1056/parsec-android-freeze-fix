#!/usr/bin/env bash
set -euo pipefail

# Parsec Android Freeze Fix: Build and Sideload Script
# Builds the optimized, 16 KB-aligned standalone APK and installs it if an ADB device is connected.

DIR="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-$DIR/build}"
DIST="${DIST:-$DIR/dist}"

mkdir -p "$WORK" "$DIST"

# If an adb device is connected and has Parsec installed, pull it for the build
if command -v adb >/dev/null 2>&1 && adb get-state >/dev/null 2>&1; then
  echo "Checking connected ADB device for tv.parsec.client..."
  P_PATH="$(adb shell pm path tv.parsec.client 2>/dev/null | tr -d '\r' | sed 's/^package://' || true)"
  if [ -n "$P_PATH" ]; then
    echo "Found tv.parsec.client on device. Pulling APKs..."
    for p in $P_PATH; do
      name="$(basename "$p")"
      adb pull "$p" "$WORK/$name" >/dev/null 2>&1
    done
  fi
fi

# Run python build pipeline
python3 "$DIR/build.py" --work-dir "$WORK" --dist-dir "$DIST"

# If ADB device is connected, install the standalone APK
if command -v adb >/dev/null 2>&1 && adb get-state >/dev/null 2>&1; then
  APK="$DIST/parsec-3.150.097.05-arm64-freeze-fix.apk"
  echo "Installing $APK to connected ADB device..."
  # Re-signing changes package signature, so uninstall first
  adb uninstall tv.parsec.client >/dev/null 2>&1 || true
  adb install -r "$APK"
  echo "Installation complete. Launch Parsec and sign back in."
else
  echo "Built APK available at: $DIST/parsec-3.150.097.05-arm64-freeze-fix.apk"
fi
