#!/usr/bin/env python3
"""Build only the addon in an EXISTING pinned Android checkout/toolchain.

No source/tool downloads, CI dispatch, AAR rewriting, library loading or media
devices. The caller preserves the official base AAR and composes the final AAR.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import zipfile

WEBRTC_REV = "19581c3352d3e77b3e9731318279772a05540bbe"
DEPOT_REV = "914c72296c0a3941d733e01e72ab3911729c2dd8"
ROOT_TARGET = "//luma_audio_factory"
ADDON_TARGET = ROOT_TARGET + ":luma_webrtc_audio"
BASE_TARGET = "//sdk/android:libjingle_peerconnection_so"
LIBRARY = "libluma_webrtc_audio.so"
BASE_LIBRARY = "libjingle_peerconnection_so.so"
SOURCES = ("BUILD.gn", "stereo_opus_factory.h", "stereo_opus_factory.cc",
           "jni_audio_factory.cc", "build_addon.py")
EXPORTS = (
    "Java_com_lumadisplay_app_LumaStereoAudioDecoderFactoryFactory_nativeCreate",
    "Java_com_lumadisplay_app_LumaStereoAudioDecoderFactoryFactory_nativeAbiVersion",
)
ABIS = {"arm64-v8a": ("arm64", 183, 2), "armeabi-v7a": ("arm", 40, 1),
        "x86": ("x86", 3, 1), "x86_64": ("x64", 62, 2)}


def digest(path):
    with Path(path).open("rb") as stream:
        return stream_digest(stream)


def stream_digest(stream):
    value = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1048576), b""):
        value.update(chunk)
    return value.hexdigest()


def run(command, cwd, capture=False):
    return subprocess.run([str(value) for value in command], cwd=cwd, check=True,
                          text=True, encoding="utf-8", capture_output=capture).stdout


def regular_file(path, maximum=268435456):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError("Required bounded regular file is missing")
    return path


def validate_args(records, abi):
    if abi not in ABIS:
        raise ValueError("Unsupported ABI")
    values = {}
    for entry in records:
        name = entry.get("name")
        if name in values:
            raise ValueError("Duplicate GN argument")
        values[name] = entry.get("current", {}).get("value")
    expected = {"target_os": '"android"', "target_cpu": json.dumps(ABIS[abi][0]),
                "is_debug": "false", "is_component_build": "false",
                "rtc_include_opus": "true", "use_remoteexec": "false"}
    if any(values.get(key) != value for key, value in expected.items()):
        raise ValueError("Base toolchain arguments do not match the required Android/Opus ABI")
    return values


def elf_machine(path, abi):
    with regular_file(path).open("rb") as stream:
        header = stream.read(64)
    if (len(header) < 52 or header[:4] != b"\x7fELF" or header[4] != ABIS[abi][2]
            or header[5] != 1 or struct.unpack_from("<H", header, 16)[0] != 3
            or struct.unpack_from("<H", header, 18)[0] != ABIS[abi][1]):
        raise ValueError("Addon is not a shared ELF for the exact base ABI")
    return ABIS[abi][1]


def base_library_hash(aar, abi):
    wanted = "jni/" + abi + "/" + BASE_LIBRARY
    with zipfile.ZipFile(regular_file(aar, 536870912)) as archive:
        matches = [item for item in archive.infolist() if item.filename == wanted]
        if len(matches) != 1 or not 0 < matches[0].file_size <= 268435456:
            raise ValueError("Base AAR must contain one exact ABI library")
        with archive.open(matches[0]) as stream:
            return stream_digest(stream)


def validate_exports(text):
    found = set()
    for line in text.splitlines():
        columns = line.split()
        if (len(columns) == 8 and columns[3:6] == ["FUNC", "GLOBAL", "DEFAULT"]
                and columns[6].isdigit() and columns[7] in EXPORTS):
            found.add(columns[7])
    if found != set(EXPORTS):
        raise ValueError("Required JNI function is not a defined public export")


def tools(source):
    platform = "win" if sys.platform == "win32" else "mac" if sys.platform == "darwin" else "linux64"
    gn = regular_file(source / "buildtools" / platform / ("gn.exe" if platform == "win" else "gn"))
    return gn


def source_copy(source, recipe):
    destination = source / "luma_audio_factory"
    if destination.is_symlink():
        raise ValueError("Addon source directory cannot be a link")
    destination.mkdir(exist_ok=True)
    hashes = {}
    for name in SOURCES:
        original = regular_file(recipe / name, 1048576)
        target = destination / name
        hashes[name] = digest(original)
        if target.exists():
            if digest(regular_file(target, 1048576)) != hashes[name]:
                raise ValueError("Existing addon source differs; use a fresh prepared checkout")
        else:
            shutil.copyfile(original, target)
    return hashes


def generate_licenses(source, build, output, gn):
    # Keep the official pinned collector and all of its missing-license checks.
    # Only its GN invocation gains the supported root-target switch. The pinned
    # source file itself is never edited.
    generator = regular_file(source / "tools_webrtc/libs/generate_licenses.py", 1048576)
    spec = importlib.util.spec_from_file_location("luma_official_license_collector", generator)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def rooted_gn(buildfile_dir, target):
        return run([gn, "desc", "--all", "--format=json", buildfile_dir, target,
                    "--root-target=" + ROOT_TARGET], source, capture=True)

    module.LicenseBuilder._run_gn = staticmethod(rooted_gn)
    notices = output / "licenses"
    notices.mkdir()
    module.LicenseBuilder([str(build)], [BASE_TARGET, ADDON_TARGET]).generate_license_text(str(notices))
    for name in ("LICENSE", "PATENTS", "AUTHORS"):
        shutil.copyfile(regular_file(source / name, 1048576), notices / name)
    return [{"path": "licenses/" + path.name, "bytes": path.stat().st_size, "sha256": digest(path)}
            for path in sorted(notices.iterdir())]


def find_readelf(source):
    suffix = "llvm-readelf.exe" if sys.platform == "win32" else "llvm-readelf"
    # The pinned Chromium Android build explicitly uses its own LLVM tooling
    # (build/config/android/config.gni), not necessarily the NDK's bin directory.
    llvm_root = source / "third_party/llvm-build/Release+Asserts"
    llvm_alias = llvm_root / "bin" / suffix
    if llvm_alias.exists():
        resolved = llvm_alias.resolve(strict=True)
        resolved.relative_to(llvm_root.resolve(strict=True))
        regular_file(resolved)
        # argv[0] selects GNU/readelf output in LLVM's readobj multicall binary.
        # Execute the verified alias, not its resolved llvm-readobj basename.
        return llvm_alias
    choices = list(source.glob("third_party/android_toolchain/ndk/toolchains/llvm/prebuilt/*/bin/" + suffix))
    if len(choices) != 1:
        raise ValueError("The existing pinned NDK readelf is required")
    # The pinned NDK may represent llvm-readelf as an in-toolchain symlink to
    # llvm-readobj. Permit only a final regular binary within that same NDK.
    resolved = choices[0].resolve(strict=True)
    resolved.relative_to((source / "third_party/android_toolchain/ndk").resolve(strict=True))
    regular_file(resolved)
    return choices[0]


def build(options):
    source = Path(options.source_root).resolve(strict=True)
    build_dir = Path(options.build_dir).resolve(strict=True)
    depot = Path(options.depot_tools).resolve(strict=True)
    base_aar = regular_file(Path(options.base_aar).resolve(strict=True), 536870912)
    output = Path(options.output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("A fresh addon output directory is required")
    if run(["git", "rev-parse", "HEAD"], source, True).strip() != WEBRTC_REV:
        raise ValueError("WebRTC pin mismatch")
    if run(["git", "rev-parse", "HEAD"], depot, True).strip() != DEPOT_REV:
        raise ValueError("depot_tools pin mismatch")
    gn = tools(source)
    args_file = regular_file(build_dir / "args.gn", 1048576)
    before_args = digest(args_file)
    base_sha = digest(base_aar)
    library_before = digest(regular_file(build_dir / BASE_LIBRARY))
    if base_library_hash(base_aar, options.abi) != library_before:
        raise ValueError("Base AAR differs from this prepared build directory")
    source_hashes = source_copy(source, Path(__file__).resolve().parent)
    run([gn, "gen", build_dir, "--root-target=" + ROOT_TARGET, "--fail-on-unused-args"], source)
    records = json.loads(run([gn, "args", build_dir, "--list", "--json",
                              "--root-target=" + ROOT_TARGET], source, True))
    values = validate_args(records, options.abi)
    if digest(args_file) != before_args:
        raise ValueError("Addon generation changed the base compiler/toolchain arguments")
    run([gn, "check", build_dir, ADDON_TARGET, "--root-target=" + ROOT_TARGET], source)
    if values.get("use_siso") == "true":
        runner = regular_file(source / "third_party/siso/cipd" / ("siso.exe" if sys.platform == "win32" else "siso"))
        command = [runner, "ninja", "-C", build_dir, "-j=" + str(options.jobs), "luma_audio_factory:luma_webrtc_audio"]
    else:
        runner = regular_file(source / "third_party/ninja" / ("ninja.exe" if sys.platform == "win32" else "ninja"))
        command = [runner, "-C", build_dir, "-j", str(options.jobs), "luma_audio_factory:luma_webrtc_audio"]
    run(command, source)
    binary = regular_file(build_dir / LIBRARY)
    machine = elf_machine(binary, options.abi)
    readelf = find_readelf(source)
    symbols = run([readelf, "--dyn-syms", "--wide", binary], source, True)
    validate_exports(symbols)
    if (digest(args_file) != before_args or digest(base_aar) != base_sha
            or digest(build_dir / BASE_LIBRARY) != library_before):
        raise ValueError("The base AAR/toolchain/library changed during addon compilation")
    output.mkdir(parents=True)
    shutil.copyfile(binary, output / LIBRARY)
    shutil.copyfile(args_file, output / "build-args.gn")
    licenses = generate_licenses(source, build_dir, output, gn)
    receipt = {"schemaVersion": 1, "kind": "luma-webrtc-android-audio-build",
               "webrtcRevision": WEBRTC_REV, "depotToolsRevision": DEPOT_REV,
               "abi": options.abi, "baseAarSha256": base_sha, "baseBuildArgsSha256": before_args,
               "nativeLibrary": LIBRARY, "nativeSha256": digest(output / LIBRARY),
               "nativeAbiVersion": 1, "sourceSha256": source_hashes,
               "elfMachine": machine, "jniExports": list(EXPORTS), "licenses": licenses}
    (output / "addon-build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print("LUMA_ANDROID_AUDIO_ADDON_BUILD_PASS; no runtime/media qualification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "build-dir", "depot-tools", "base-aar", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--abi", choices=ABIS, required=True)
    parser.add_argument("--jobs", type=int, choices=range(1, 33), default=4)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
