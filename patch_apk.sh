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
if command -v apktool >/dev/null 2>&1 && python3 -c "import PIL" 2>/dev/null; then
  echo "  Updating adaptive & monochrome launcher icons via apktool..."
  rm -rf "$WORK/base_apktool"
  apktool d -s -f "$WORK/base.apk" -o "$WORK/base_apktool" >/dev/null
  python3 - "$WORK/base_apktool" << 'ICONEOF'
import os, sys
from PIL import Image

apkdir = sys.argv[1]
fg = Image.open(f"{apkdir}/res/drawable/ic_launcher_foreground.png").convert("RGBA").resize((432, 432), Image.LANCZOS)
fg.save(f"{apkdir}/res/drawable/ic_launcher_foreground.png")
Image.new("RGB", (432, 432), (12, 12, 13)).save(f"{apkdir}/res/drawable/ic_launcher_background.png")
_, _, _, a = fg.split()
Image.merge("RGBA", (Image.new("L", fg.size, 255),) * 3 + (a,)).save(f"{apkdir}/res/drawable/ic_launcher_monochrome.png")
ICONEOF
  rm -rf "$WORK/base_apktool/res/mipmap-anydpi"
  mkdir -p "$WORK/base_apktool/res/mipmap-anydpi-v26"
  cat > "$WORK/base_apktool/res/mipmap-anydpi-v26/ic_launcher.xml" << 'XMLEOF'
<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@drawable/ic_launcher_background" />
    <foreground android:drawable="@drawable/ic_launcher_foreground" />
    <monochrome android:drawable="@drawable/ic_launcher_monochrome" />
</adaptive-icon>
XMLEOF
  cp "$WORK/base_apktool/res/mipmap-anydpi-v26/ic_launcher.xml" \
     "$WORK/base_apktool/res/mipmap-anydpi-v26/ic_launcher_round.xml"
  apktool b "$WORK/base_apktool" -o "$WORK/base_unsigned.apk" >/dev/null
else
  python3 - "$WORK" << 'EOF'
import sys, zipfile
work = sys.argv[1]
with zipfile.ZipFile(f"{work}/base.apk", "r") as zin, zipfile.ZipFile(f"{work}/base_unsigned.apk", "w") as zout:
    for item in zin.infolist():
        if item.filename.startswith("META-INF/"):
            continue
        zi = zipfile.ZipInfo(item.filename)
        zi.compress_type = zipfile.ZIP_STORED if (item.filename == "resources.arsc" or item.compress_type == zipfile.ZIP_STORED) else zipfile.ZIP_DEFLATED
        zout.writestr(zi, zin.read(item.filename))
EOF
fi

python3 - "$WORK" << 'EOF'
import sys, zipfile
work = sys.argv[1]
with zipfile.ZipFile(f"{work}/split_config.arm64_v8a.apk", "r") as zin, zipfile.ZipFile(f"{work}/split_arm64_unsigned.apk", "w") as zout:
    patched = open(f"{work}/libmain_patched.so", "rb").read()
    for item in zin.infolist():
        if item.filename.startswith("META-INF/"):
            continue
        data = patched if item.filename == "lib/arm64-v8a/libmain.so" else zin.read(item.filename)
        zi = zipfile.ZipInfo(item.filename)
        zi.compress_type = zipfile.ZIP_STORED if (item.filename.endswith(".so") or item.compress_type == zipfile.ZIP_STORED) else zipfile.ZIP_DEFLATED
        zout.writestr(zi, data)
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
