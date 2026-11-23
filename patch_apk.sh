#!/usr/bin/env bash
set -euo pipefail

# Pulls tv.parsec.client from a connected adb device, patches libmain.so,
# re-signs both split APKs, and sideloads them back onto the device.
# Requires: adb, python3, zipalign, apksigner, keytool.

WORK="${WORK:-./work}"
mkdir -p "$WORK"

echo "[1/5] Pulling Parsec APKs from device..."
for p in $(adb shell pm path tv.parsec.client | tr -d '\r' | sed 's/^package://'); do
  name="$(basename "$p")"
  adb pull "$p" "$WORK/$name" >/dev/null
done

echo "[2/5] Extracting and patching libmain.so..."
unzip -q -o "$WORK/split_config.arm64_v8a.apk" "lib/arm64-v8a/libmain.so" -d "$WORK/arm64"
python3 "$(dirname "$0")/patch_parsec.py" \
  "$WORK/arm64/lib/arm64-v8a/libmain.so" \
  "$WORK/libmain_patched.so"

echo "[3/5] Repacking APKs..."
python3 - "$WORK" << 'EOF'
import sys, zipfile
work = sys.argv[1]

def repack(src, dst, replace_map=None):
    replace_map = replace_map or {}
    with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            if item.filename.startswith("META-INF/"):
                continue
            data = replace_map[item.filename] if item.filename in replace_map else zin.read(item.filename)
            zi = zipfile.ZipInfo(item.filename)
            if item.filename.endswith(".so") or item.filename == "resources.arsc" or item.compress_type == zipfile.ZIP_STORED:
                zi.compress_type = zipfile.ZIP_STORED
            else:
                zi.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(zi, data)

repack(f"{work}/base.apk", f"{work}/base_unsigned.apk")
repack(f"{work}/split_config.arm64_v8a.apk", f"{work}/split_arm64_unsigned.apk", {
    "lib/arm64-v8a/libmain.so": open(f"{work}/libmain_patched.so", "rb").read()
})
EOF

echo "[4/5] Aligning and signing APKs..."
zipalign -P 16 -f 4 "$WORK/base_unsigned.apk" "$WORK/base_aligned.apk"
zipalign -P 16 -f 4 "$WORK/split_arm64_unsigned.apk" "$WORK/split_arm64_aligned.apk"

if [ ! -f "$WORK/patch.keystore" ]; then
  keytool -genkeypair -keystore "$WORK/patch.keystore" -alias patchkey \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass android -keypass android -dname "CN=ParsecPatch,O=Local,C=US" >/dev/null 2>&1
fi

apksigner sign --ks "$WORK/patch.keystore" --ks-pass pass:android --key-pass pass:android --out "$WORK/base_signed.apk" "$WORK/base_aligned.apk"
apksigner sign --ks "$WORK/patch.keystore" --ks-pass pass:android --key-pass pass:android --out "$WORK/split_arm64_signed.apk" "$WORK/split_arm64_aligned.apk"

echo "[5/5] Uninstalling old signature and installing patched APKs..."
adb uninstall tv.parsec.client || true
adb install-multiple "$WORK/base_signed.apk" "$WORK/split_arm64_signed.apk"
echo "Done. Launch Parsec and sign back in."
