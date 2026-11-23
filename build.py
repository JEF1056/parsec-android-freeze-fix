#!/usr/bin/env python3
"""Build production-optimized, 16 KB-aligned, ready-to-install Parsec Android APKs.

This script:
1. Acquires Parsec Android 3.150.097.05 (from local files or auto-download).
2. Patches libmain.so via patch_parsec.py to fix the AMediaCodec video freeze deadlock.
3. Modifies AndroidManifest.xml and merges libmain.so into base.apk to produce a single
   standalone, 1-tap installable APK (no split APK installer or adb needed).
4. Upgrades launcher icons to Android adaptive + monochrome specs (432x432).
5. Stores native libraries uncompressed and aligns them to 16 KB pages (zipalign -P 16 -f 4)
   for full Android 15+ 16 KB page-size support and zero-copy mmap.
6. Signs with apksigner (v1, v2, v3 schemes) using a custom or auto-generated keystore.
7. Produces both a standalone APK and a split APKs zip, along with SHA256 checksums.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import patch_parsec

VERSION_STRING = "3.150.097.05"
PACKAGE_NAME = "tv.parsec.client"
APK_DOWNLOAD_URL = "https://d.apkpure.net/b/XAPK/tv.parsec.client?version=latest"


def setup_environment() -> None:
    """Ensure JAVA_HOME and PATH are properly set for Android tools."""
    # If JAVA_HOME is not set, check known locations
    if not os.environ.get("JAVA_HOME"):
        brew_jdk = Path("/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home")
        if brew_jdk.exists():
            os.environ["JAVA_HOME"] = str(brew_jdk)

    # Ensure tool directories are in PATH
    current_path = os.environ.get("PATH", "")
    extra_paths = [
        "/opt/homebrew/opt/openjdk/bin",
        "/opt/homebrew/share/android-commandlinetools/build-tools/36.0.0",
        "/opt/homebrew/share/android-commandlinetools/build-tools/37.0.0",
        "/opt/homebrew/share/android-commandlinetools/build-tools/34.0.0",
    ]
    for p in extra_paths:
        if Path(p).exists() and p not in current_path:
            os.environ["PATH"] = f"{p}:{os.environ['PATH']}"


def find_tool(name: str) -> str:
    """Find a build tool on PATH or in Android SDK directories."""
    # Check PATH first
    found = shutil.which(name)
    if found:
        return found

    # Search common Android SDK locations
    search_dirs = []
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(env_var)
        if val:
            search_dirs.append(Path(val))

    search_dirs.extend([
        Path("/opt/homebrew/share/android-commandlinetools"),
        Path("/opt/homebrew/opt/android-commandlinetools"),
        Path("/usr/local/share/android-sdk"),
        Path(os.path.expanduser("~/Library/Android/sdk")),
    ])

    for base in search_dirs:
        if not base.exists():
            continue
        candidates = sorted(base.glob(f"build-tools/*/{name}"), reverse=True)
        if candidates:
            return str(candidates[0])

    # Check Homebrew opt for standalone tools (e.g. apktool, openjdk)
    brew_opt = Path(f"/opt/homebrew/opt/{name}/bin/{name}")
    if brew_opt.exists():
        return str(brew_opt)

    sys.exit(f"Error: Required tool '{name}' not found on PATH or in Android SDK. Please install it.")


def run_cmd(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command with clear error reporting."""
    res = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if res.returncode != 0:
        cmd_str = " ".join(cmd)
        sys.exit(f"Command failed ({res.returncode}):\n{cmd_str}\n\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
    return res


def compute_sha256(filepath: Path) -> str:
    """Compute hex SHA256 of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def acquire_source_apks(work_dir: Path, base_arg: str | None, split_arg: str | None, xapk_arg: str | None) -> tuple[Path, Path]:
    """Obtain base.apk and config.arm64_v8a.apk from arguments, local cache, or download."""
    base_target = work_dir / "base.apk"
    split_target = work_dir / "split_config.arm64_v8a.apk"

    if base_arg and split_arg and Path(base_arg).exists() and Path(split_arg).exists():
        shutil.copyfile(base_arg, base_target)
        shutil.copyfile(split_arg, split_target)
        return base_target, split_target

    # Check if orig/ directory exists
    repo_orig = Path(__file__).parent / "orig"
    if (repo_orig / "base.apk").exists() and (repo_orig / "split_config.arm64_v8a.apk").exists():
        shutil.copyfile(repo_orig / "base.apk", base_target)
        shutil.copyfile(repo_orig / "split_config.arm64_v8a.apk", split_target)
        return base_target, split_target

    if xapk_arg and Path(xapk_arg).exists():
        xapk_path = Path(xapk_arg)
    else:
        # Download XAPK
        xapk_path = work_dir / "parsec.xapk"
        if not xapk_path.exists():
            print(f"Downloading Parsec Android {VERSION_STRING} from {APK_DOWNLOAD_URL}...")
            req = urllib.request.Request(
                APK_DOWNLOAD_URL,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp, open(xapk_path, "wb") as out:
                shutil.copyfileobj(resp, out)
            print(f"Downloaded {xapk_path.stat().st_size} bytes.")

    # Extract base and split from XAPK
    with zipfile.ZipFile(xapk_path, "r") as z:
        namelist = z.namelist()
        base_name = next((n for n in ("tv.parsec.client.apk", "base.apk") if n in namelist), None)
        split_name = next((n for n in ("config.arm64_v8a.apk", "split_config.arm64_v8a.apk") if n in namelist), None)

        if not base_name or not split_name:
            sys.exit(f"Error: XAPK does not contain base and arm64 split APKs. Found: {namelist}")

        with open(base_target, "wb") as f:
            f.write(z.read(base_name))
        with open(split_target, "wb") as f:
            f.write(z.read(split_name))

    return base_target, split_target


def ensure_keystore(keystore_path: Path, storepass: str, keypass: str, key_alias: str) -> None:
    """Generate a self-signed release keystore if it doesn't already exist."""
    if keystore_path.exists():
        return

    keytool = find_tool("keytool")
    print(f"Generating release signing key at {keystore_path}...")
    run_cmd([
        keytool, "-genkeypair",
        "-keystore", str(keystore_path),
        "-alias", key_alias,
        "-keyalg", "RSA",
        "-keysize", "2048",
        "-validity", "10000",
        "-storepass", storepass,
        "-keypass", keypass,
        "-dname", "CN=ParsecFreezeFix,O=ParsecCommunity,C=US",
    ])


def update_adaptive_icons(apk_dir: Path) -> None:
    """Generate modern 432x432 adaptive and monochrome launcher icons."""
    try:
        from PIL import Image
    except ImportError:
        print("Pillow not installed; skipping adaptive icon update.")
        return

    fg_path = apk_dir / "res" / "drawable" / "ic_launcher_foreground.png"
    if not fg_path.exists():
        return

    print("Generating Android adaptive + monochrome launcher icons...")
    fg = Image.open(fg_path).convert("RGBA").resize((432, 432), Image.LANCZOS)
    fg.save(fg_path)

    bg_path = apk_dir / "res" / "drawable" / "ic_launcher_background.png"
    Image.new("RGB", (432, 432), (12, 12, 13)).save(bg_path)

    mono_path = apk_dir / "res" / "drawable" / "ic_launcher_monochrome.png"
    _, _, _, a = fg.split()
    Image.merge("RGBA", (Image.new("L", fg.size, 255),) * 3 + (a,)).save(mono_path)

    legacy_dir = apk_dir / "res" / "mipmap-anydpi"
    if legacy_dir.exists():
        shutil.rmtree(legacy_dir)

    v26_dir = apk_dir / "res" / "mipmap-anydpi-v26"
    v26_dir.mkdir(parents=True, exist_ok=True)

    xml_content = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@drawable/ic_launcher_background" />
    <foreground android:drawable="@drawable/ic_launcher_foreground" />
    <monochrome android:drawable="@drawable/ic_launcher_monochrome" />
</adaptive-icon>
"""
    (v26_dir / "ic_launcher.xml").write_text(xml_content, encoding="utf-8")
    (v26_dir / "ic_launcher_round.xml").write_text(xml_content, encoding="utf-8")


def strip_split_metadata(manifest_path: Path) -> None:
    """Modify AndroidManifest.xml so the APK installs standalone without split requirements."""
    manifest = manifest_path.read_text(encoding="utf-8")

    # Remove split-related attributes from <manifest>
    manifest = re.sub(r'\s+android:requiredSplitTypes="[^"]*"', '', manifest)
    manifest = re.sub(r'\s+android:splitTypes="[^"]*"', '', manifest)

    # Remove Google Play split requirement meta-tags
    manifest = re.sub(r'<meta-data\s+android:name="com\.android\.vending\.splits[^"]*"\s+[^>]*/>\s*', '', manifest)
    manifest = re.sub(r'<meta-data\s+android:name="com\.android\.vending\.derived\.apk\.id"\s+[^>]*/>\s*', '', manifest)

    manifest_path.write_text(manifest, encoding="utf-8")


def repack_stored_apk(in_apk: Path, out_apk: Path, extra_files: dict[str, bytes] | None = None) -> None:
    """Repack APK with uncompressed (.so, .arsc) files for zero-copy mmap and strip META-INF."""
    extra = extra_files or {}
    with zipfile.ZipFile(in_apk, "r") as zin, zipfile.ZipFile(out_apk, "w") as zout:
        seen = set()
        for item in zin.infolist():
            if item.filename.startswith("META-INF/"):
                continue

            data = extra.get(item.filename, zin.read(item.filename))
            seen.add(item.filename)

            zi = zipfile.ZipInfo(item.filename)
            # Store uncompressed for .so (Android 15+ 16 KB mmap) and resources.arsc
            if item.filename.endswith(".so") or item.filename == "resources.arsc":
                zi.compress_type = zipfile.ZIP_STORED
            else:
                zi.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(zi, data)

        for filename, data in extra.items():
            if filename in seen:
                continue
            zi = zipfile.ZipInfo(filename)
            zi.compress_type = zipfile.ZIP_STORED if filename.endswith(".so") else zipfile.ZIP_DEFLATED
            zout.writestr(zi, data)


def sign_and_align_apk(
    raw_apk: Path,
    out_apk: Path,
    keystore: Path,
    storepass: str,
    keypass: str,
    key_alias: str,
    zipalign_bin: str,
    apksigner_bin: str,
) -> None:
    """Align to 16 KB page boundary and sign with apksigner (v1, v2, v3)."""
    aligned_apk = raw_apk.with_suffix(".aligned.apk")

    # 16 KB page alignment is mandatory for Android 15+ devices
    print(f"Aligning {raw_apk.name} to 16 KB page boundary...")
    run_cmd([zipalign_bin, "-P", "16", "-f", "4", str(raw_apk), str(aligned_apk)])

    print(f"Signing {out_apk.name} with apksigner...")
    run_cmd([
        apksigner_bin, "sign",
        "--ks", str(keystore),
        "--ks-pass", f"pass:{storepass}",
        "--key-pass", f"pass:{keypass}",
        "--ks-key-alias", key_alias,
        "--min-sdk-version", "28",
        "--v1-signing-enabled", "true",
        "--v2-signing-enabled", "true",
        "--v3-signing-enabled", "true",
        "--out", str(out_apk),
        str(aligned_apk),
    ])

    # Verify signature
    verify_res = run_cmd([apksigner_bin, "verify", "--verbose", "--min-sdk-version", "28", str(out_apk)])
    if "Verifies" not in verify_res.stdout:
        sys.exit(f"Signature verification failed for {out_apk}")

    # Verify 16 KB alignment
    check_align = run_cmd([zipalign_bin, "-c", "-P", "16", "-v", "4", str(out_apk)])
    if "Verification successful" not in check_align.stdout:
        sys.exit(f"16 KB alignment check failed for {out_apk}")

    aligned_apk.unlink(missing_ok=True)


def build(args: argparse.Namespace) -> None:
    setup_environment()
    work_dir = Path(args.work_dir).resolve()
    dist_dir = Path(args.dist_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    # Locate tools
    zipalign = find_tool("zipalign")
    apksigner = find_tool("apksigner")
    apktool = find_tool("apktool")

    print("=== Parsec Android Freeze Fix Build Pipeline ===")
    print(f"Work dir: {work_dir}")
    print(f"Dist dir: {dist_dir}")
    print(f"Using zipalign:  {zipalign}")
    print(f"Using apksigner: {apksigner}")
    print(f"Using apktool:   {apktool}")

    # 1. Acquire source APKs
    base_apk, split_apk = acquire_source_apks(work_dir, args.base_apk, args.split_apk, args.xapk)

    # 2. Extract and patch libmain.so
    print("\n[1/5] Extracting and patching libmain.so...")
    so_orig = work_dir / "libmain_orig.so"
    so_patched = work_dir / "libmain.so"

    with zipfile.ZipFile(split_apk, "r") as z:
        with open(so_orig, "wb") as f:
            f.write(z.read("lib/arm64-v8a/libmain.so"))

    patch_parsec.patch_elf(str(so_orig), str(so_patched))

    # Also copy patched libmain.so to dist for modders/developers
    shutil.copyfile(so_patched, dist_dir / "libmain_patched.so")

    # 3. Setup keystore
    keystore_path = Path(args.keystore).resolve() if args.keystore else work_dir / "release.keystore"
    ensure_keystore(keystore_path, args.storepass, args.keypass, args.key_alias)

    # 4. Build Standalone 1-tap APK
    print("\n[2/5] Building standalone 1-tap APK (decoding base.apk with apktool)...")
    base_apktool_dir = work_dir / "base_apktool"
    if base_apktool_dir.exists():
        shutil.rmtree(base_apktool_dir)

    run_cmd([apktool, "d", "-s", "-f", str(base_apk), "-o", str(base_apktool_dir)])

    # Patch AndroidManifest.xml and launcher icons
    strip_split_metadata(base_apktool_dir / "AndroidManifest.xml")
    update_adaptive_icons(base_apktool_dir)

    # Rebuild base with apktool
    apktool_built = work_dir / "base_apktool_built.apk"
    print("Rebuilding APK with apktool...")
    run_cmd([apktool, "b", str(base_apktool_dir), "-o", str(apktool_built)])

    # Inject patched libmain.so into standalone APK with ZIP_STORED compression
    print("Injecting uncompressed 16 KB-aligned libmain.so into standalone APK...")
    patched_so_bytes = so_patched.read_bytes()
    standalone_raw = work_dir / "standalone_raw.apk"
    repack_stored_apk(apktool_built, standalone_raw, extra_files={"lib/arm64-v8a/libmain.so": patched_so_bytes})

    standalone_out = dist_dir / f"parsec-{VERSION_STRING}-arm64-freeze-fix.apk"
    print("\n[3/5] Aligning (16 KB) and signing standalone APK...")
    sign_and_align_apk(
        standalone_raw,
        standalone_out,
        keystore_path,
        args.storepass,
        args.keypass,
        args.key_alias,
        zipalign,
        apksigner,
    )

    # 5. Build Split APKs (optional bundle for split installer users)
    if not args.skip_splits:
        print("\n[4/5] Building signed split APKs package...")
        split_base_raw = work_dir / "split_base_raw.apk"
        repack_stored_apk(apktool_built, split_base_raw)

        split_arm64_raw = work_dir / "split_arm64_raw.apk"
        repack_stored_apk(split_apk, split_arm64_raw, extra_files={"lib/arm64-v8a/libmain.so": patched_so_bytes})

        split_base_signed = work_dir / "base.apk"
        split_arm64_signed = work_dir / "split_config.arm64_v8a.apk"

        sign_and_align_apk(split_base_raw, split_base_signed, keystore_path, args.storepass, args.keypass, args.key_alias, zipalign, apksigner)
        sign_and_align_apk(split_arm64_raw, split_arm64_signed, keystore_path, args.storepass, args.keypass, args.key_alias, zipalign, apksigner)

        splits_zip = dist_dir / f"parsec-{VERSION_STRING}-arm64-splits.zip"
        with zipfile.ZipFile(splits_zip, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(split_base_signed, "base.apk")
            z.write(split_arm64_signed, "split_config.arm64_v8a.apk")
        print(f"Created {splits_zip.name}")

    # 6. Generate Checksums
    print("\n[5/5] Generating SHA256 checksums...")
    checksums = []
    for item in sorted(dist_dir.iterdir()):
        if item.is_file() and not item.name.endswith(".sha256"):
            csum = compute_sha256(item)
            csum_line = f"{csum}  {item.name}\n"
            checksums.append(csum_line)
            (dist_dir / f"{item.name}.sha256").write_text(csum_line, encoding="utf-8")
            print(f"  {csum[:16]}... {item.name} ({item.stat().st_size:,} bytes)")

    (dist_dir / "SHA256SUMS.txt").write_text("".join(checksums), encoding="utf-8")

    print("\n✓ Build completed successfully!")
    print(f"Output APK: {standalone_out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-dir", default="./build", help="Directory for intermediate files (default: ./build)")
    parser.add_argument("--dist-dir", default="./dist", help="Directory for release artifacts (default: ./dist)")
    parser.add_argument("--xapk", help="Path to input XAPK bundle")
    parser.add_argument("--base-apk", help="Path to base.apk")
    parser.add_argument("--split-apk", help="Path to split_config.arm64_v8a.apk")
    parser.add_argument("--keystore", help="Path to custom keystore (auto-generated if omitted)")
    parser.add_argument("--storepass", default="android", help="Keystore password (default: android)")
    parser.add_argument("--keypass", default="android", help="Key password (default: android)")
    parser.add_argument("--key-alias", default="parsec-release", help="Key alias (default: parsec-release)")
    parser.add_argument("--skip-splits", action="store_true", help="Skip generating the split APKs zip")

    build(parser.parse_args())


if __name__ == "__main__":
    main()
