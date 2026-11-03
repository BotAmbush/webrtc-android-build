#!/usr/bin/env python3
"""Compose the verified official AAR and same-toolchain stereo addon, without execution."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import zipfile

WEBRTC = "19581c3352d3e77b3e9731318279772a05540bbe"
DEPOT = "914c72296c0a3941d733e01e72ab3911729c2dd8"
NATIVE = "libjingle_peerconnection_so.so"
ADDON = "libluma_webrtc_audio.so"
MACHINES = {"arm64-v8a": 183, "x86_64": 62}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest().upper()


def read(path, maximum=536870912):
    path = Path(path)
    require(not path.is_symlink() and path.is_file() and 0 < path.stat().st_size <= maximum,
            "Missing or unbounded regular input file")
    return path.read_bytes()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate receipt field")
        result[key] = value
    return result


def receipt(path):
    return json.loads(read(path, 1048576).decode("utf-8-sig"), object_pairs_hook=unique_object)


def member(root, name):
    require(isinstance(name, str) and name and "\\" not in name, "Invalid manifest path")
    relative = PurePosixPath(name)
    require(not relative.is_absolute() and all(p not in ("", ".", "..") for p in relative.parts)
            and ":" not in name, "Manifest path escapes its root")
    path = root.joinpath(*relative.parts)
    require(not any(p.is_symlink() for p in (path, *path.parents) if p != root.parent),
            "Linked manifest input")
    require(path.resolve().is_relative_to(root.resolve()), "Manifest path escapes its root")
    return path


def same_hash(data, expected):
    require(isinstance(expected, str) and len(expected) == 64 and sha(data) == expected.upper(),
            "Input differs from its recorded SHA-256")


def pins(value, kind, abi):
    require(value.get("schemaVersion") == 1 and value.get("kind") == kind
            and value.get("webrtcRevision") == WEBRTC
            and value.get("depotToolsRevision") == DEPOT and value.get("abi") == abi,
            "Build receipt source or ABI mismatch")


def elf(data, abi):
    require(len(data) >= 64 and data[:6] == b"\x7fELF\x02\x01"
            and struct.unpack_from("<H", data, 16)[0] == 3
            and struct.unpack_from("<H", data, 18)[0] == MACHINES[abi],
            "Native dependency is not the exact shared ELF ABI")


def compose(base_root, addon_root, output, abi):
    require(abi in MACHINES, "Unsupported receiver ABI")
    base_root, addon_root, output = Path(base_root), Path(addon_root), Path(output)
    require(not output.exists() and not output.is_symlink(), "Use a fresh composed output directory")
    base_receipt_bytes = read(base_root / "build-receipt.json", 1048576)
    base = receipt(base_root / "build-receipt.json")
    addon_receipt_bytes = read(addon_root / "addon-build-receipt.json", 1048576)
    addon = receipt(addon_root / "addon-build-receipt.json")
    pins(base, "official-webrtc-android-build", abi)
    pins(addon, "luma-webrtc-android-audio-build", abi)
    require(isinstance(base.get("files"), dict) and 1 <= len(base["files"]) <= 256,
            "Invalid official file manifest")
    # Validate the complete official output before composing, not just the AAR.
    for name, expected in base["files"].items():
        same_hash(read(member(base_root, name)), expected)
    aar_name = base.get("aar")
    require(isinstance(aar_name, str) and PurePosixPath(aar_name).name == aar_name,
            "Official AAR name must be a basename")
    aar = read(member(base_root, aar_name))
    same_hash(aar, base.get("aarSha256"))
    same_hash(aar, addon.get("baseAarSha256"))
    require(addon.get("nativeLibrary") == ADDON and addon.get("nativeAbiVersion") == 1,
            "Unexpected stereo factory ABI")
    source_hashes = addon.get("sourceSha256")
    expected_sources = {"BUILD.gn", "stereo_opus_factory.h", "stereo_opus_factory.cc",
                        "jni_audio_factory.cc", "build_addon.py"}
    require(isinstance(source_hashes, dict) and set(source_hashes) == expected_sources
            and all(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value)
                    for value in source_hashes.values()), "Missing exact addon source hashes")
    addon_binary = read(addon_root / ADDON, 268435456)
    same_hash(addon_binary, addon.get("nativeSha256"))
    elf(addon_binary, abi)
    arguments = read(base_root / "build-args.gn", 1048576)
    same_hash(arguments, addon.get("baseBuildArgsSha256"))
    require(arguments == read(addon_root / "build-args.gn", 1048576),
            "Addon GN arguments differ from the official build")
    required_exports = {
        "Java_com_lumadisplay_app_LumaStereoAudioDecoderFactoryFactory_nativeCreate",
        "Java_com_lumadisplay_app_LumaStereoAudioDecoderFactoryFactory_nativeAbiVersion",
    }
    require(isinstance(addon.get("jniExports"), list) and len(addon["jniExports"]) == 2
            and set(addon["jniExports"]) == required_exports,
            "Missing exact owned-factory JNI exports")
    require(addon.get("elfMachine") == MACHINES[abi], "Addon machine receipt mismatch")
    notices = addon.get("licenses")
    require(isinstance(notices, list) and 4 <= len(notices) <= 64, "Missing addon license closure")
    notice_bytes = {}
    for notice in notices:
        name = notice.get("path")
        require(name not in notice_bytes and isinstance(name, str) and name.startswith("licenses/"),
                "Invalid or duplicate license path")
        content = read(member(addon_root, name), 16777216)
        require(len(content) == notice.get("bytes"), "License byte count mismatch")
        same_hash(content, notice.get("sha256"))
        notice_bytes[name] = content
    for name in ("LICENSE", "PATENTS", "AUTHORS"):
        require(notice_bytes.get("licenses/" + name) == read(base_root / "licenses" / name, 1048576),
                "Official source notices differ between builds")
    require("licenses/LICENSE.md" in notice_bytes, "Combined target license closure is absent")
    with zipfile.ZipFile(io.BytesIO(aar)) as original:
        infos = original.infolist()
        require(3 <= len(infos) <= 64 and len({i.filename for i in infos}) == len(infos),
                "AAR has duplicate or excessive entries")
        entries = {}
        total = 0
        for entry in infos:
            member(base_root, entry.filename)  # Validate names without extracting.
            require(not entry.is_dir() and entry.file_size <= 268435456,
                    "AAR contains an unsupported entry")
            total += entry.file_size
            require(total <= 536870912, "AAR expands beyond its bound")
            entries[entry.filename] = original.read(entry)
    native_path = "jni/" + abi + "/" + NATIVE
    addon_path = "jni/" + abi + "/" + ADDON
    require(native_path in entries and "classes.jar" in entries and "AndroidManifest.xml" in entries,
            "Official AAR is incomplete")
    require([name for name in entries if name.startswith("jni/")] == [native_path],
            "Official AAR contains unexpected native libraries")
    elf(entries[native_path], abi)
    require(addon_path not in entries, "AAR already contains an addon")
    with zipfile.ZipFile(io.BytesIO(entries["classes.jar"])) as classes:
        names = classes.namelist()
        require(len(names) == len(set(names)), "Duplicate Java classes")
        for name in ("PeerConnection", "PeerConnectionFactory", "DefaultVideoDecoderFactory", "AudioDecoderFactoryFactory", "EglRenderer"):
            require("org/webrtc/" + name + ".class" in names, "Official Java API is missing")
    output.mkdir(parents=True)
    combined_name = "libwebrtc-" + WEBRTC + "-" + abi + "-stereo.aar"
    with zipfile.ZipFile(output / combined_name, "w", compression=zipfile.ZIP_DEFLATED) as merged:
        for name, content in entries.items():
            merged.writestr(name, content)
        merged.writestr(addon_path, addon_binary)
    (output / "base-build-receipt.json").write_bytes(base_receipt_bytes)
    (output / "addon-build-receipt.json").write_bytes(addon_receipt_bytes)
    (output / "build-args.gn").write_bytes(arguments)
    (output / "LICENSE.md").write_bytes(notice_bytes["licenses/LICENSE.md"])
    for name, content in notice_bytes.items():
        target = member(output, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    for name in ("DEPS", "source-revisions.txt"):
        shutil.copyfile(base_root / name, output / name)
    files = {p.relative_to(output).as_posix(): sha(p.read_bytes())
             for p in sorted(output.rglob("*")) if p.is_file()}
    result = {"schemaVersion": 1, "kind": "luma-webrtc-android-stereo-build",
              "webrtcRevision": WEBRTC, "depotToolsRevision": DEPOT, "abi": abi,
              "aar": combined_name, "aarSha256": files[combined_name], "addonAbi": 1,
              "baseAarSha256": sha(aar), "baseBuildReceiptSha256": sha(base_receipt_bytes),
              "addonBuildReceiptSha256": sha(addon_receipt_bytes), "addonSha256": sha(addon_binary),
              "nativeLibraries": {native_path: sha(entries[native_path]), addon_path: sha(addon_binary)},
              "nativeOrDeviceExecution": False, "hardwareQualified": False, "files": files}
    receipt_file = output / "build-receipt.json"
    receipt_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    files["build-receipt.json"] = sha(receipt_file.read_bytes())
    (output / "SHA256SUMS").write_text("".join(value + "  " + name + "\n" for name, value in sorted(files.items())), encoding="ascii")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("base", "addon", "output"):
        parser.add_argument("--" + argument, required=True)
    parser.add_argument("--abi", choices=MACHINES, required=True)
    args = parser.parse_args()
    result = compose(args.base, args.addon, args.output, args.abi)
    print(json.dumps({"aar": result["aar"], "aarSha256": result["aarSha256"],
                      "nativeOrDeviceExecution": False}))
