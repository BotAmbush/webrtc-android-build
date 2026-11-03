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
from pathlib import Path, PurePosixPath
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


def run(command, cwd, capture=False, depot=None):
    # Preparation and addon compilation can be separate shell steps. Preserve
    # the pinned depot child-tool lookup (gclient invokes cipd) without changing
    # this process's or the machine's PATH.
    environment = None
    if depot is not None:
        environment = os.environ.copy()
        environment["PATH"] = str(depot) + os.pathsep + environment.get("PATH", "")
    try:
        return subprocess.run([str(value) for value in command], cwd=cwd, check=True,
                              text=True, encoding="utf-8", capture_output=capture,
                              env=environment).stdout
    except subprocess.CalledProcessError as error:
        if capture:
            for output in (error.stdout, error.stderr):
                if output:
                    print(output[-65536:], file=sys.stderr, flush=True)
        raise


def regular_file(path, maximum=268435456):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError("Required bounded regular file is missing")
    return path


def effective_args(records):
    values = {}
    for entry in records:
        name = entry.get("name")
        if name in values:
            raise ValueError("Duplicate GN argument")
        # GN emits `current` only for an explicit override. An unchanged
        # declared argument has just `default`, even though it is effective.
        selected = entry.get("current") if "current" in entry else entry.get("default")
        values[name] = selected.get("value") if isinstance(selected, dict) else None
    return values


def validate_args(records, abi):
    if abi not in ABIS:
        raise ValueError("Unsupported ABI")
    values = effective_args(records)
    expected = {"target_os": '"android"', "target_cpu": json.dumps(ABIS[abi][0]),
                "is_debug": "false", "is_component_build": "false",
                "rtc_include_opus": "true", "use_remoteexec": "false"}
    mismatches = [key for key, value in expected.items() if values.get(key) != value]
    if values.get("use_siso") not in ("true", "false"):
        mismatches.append("use_siso")
    if mismatches:
        raise ValueError("Required Android/Opus GN arguments differ or are missing: " + ", ".join(mismatches))
    return values


def elf_machine(path, abi):
    with regular_file(path).open("rb") as stream:
        header = stream.read(64)
    return validate_elf_header(header, abi)


def validate_elf_header(header, abi):
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


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate official receipt field")
        result[key] = value
    return result


def manifest_member(root, name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("Invalid official asset path")
    relative = PurePosixPath(name)
    if (relative.is_absolute() or relative.as_posix() != name
            or any(part in ("", ".", "..") for part in relative.parts)):
        raise ValueError("Official asset path escapes its root")
    at = root
    for part in relative.parts:
        at = at / part
        if at.is_symlink():
            raise ValueError("Official assets cannot be links")
    at.resolve(strict=True).relative_to(root.resolve(strict=True))
    return regular_file(at, 536870912)


def revision_map(text):
    result = {}
    for line in text.splitlines():
        name, separator, value = line.strip().partition(": ")
        path, virtual_separator, virtual = name.partition(":")
        if (not separator or name in result or not value
                or not re.fullmatch(r"[A-Za-z0-9_./+-]+", path)
                or path != "src" and not path.startswith("src/")
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise ValueError("Invalid or duplicate source revision entry")
        if virtual_separator:
            # gclient uses path:package or path:object-id keys for CIPD/GCS.
            # These are evidence keys, NOT paths to filesystem Git checkouts.
            if (not re.fullmatch(r"[A-Za-z0-9_./${}+-]+", virtual)
                    or any(part in ("", ".", "..") for part in virtual.split("/"))
                    or not (re.fullmatch(r"https://chrome-infra-packages\.appspot\.com/p/[A-Za-z0-9_./+-]+", value)
                            or re.fullmatch(r"gs://[A-Za-z0-9_.+-]+/[A-Za-z0-9_./+-]+", value))):
                raise ValueError("Invalid CIPD/GCS revision evidence")
        elif not re.fullmatch(r"https://\S+@[0-9a-fA-F]{40}", value):
            raise ValueError("Git revision evidence requires an exact commit")
        result[name] = value
    if not result:
        raise ValueError("Source revisions are missing")
    return result


def require_clean_checkouts(source, depot, revisions):
    # HEAD-inclusive diff covers both staged and unstaged tracked changes. An
    # exact revinfo map alone says nothing about modified header/codec bytes.
    repositories = [(Path(depot).resolve(strict=True), DEPOT_REV)]
    for name, value in revision_map(revisions).items():
        if ":" in name:
            continue  # Validated virtual CIPD/GCS evidence is never path-resolved.
        revision = value.rsplit("@", 1)[-1]
        repository = (Path(source).parent / name).resolve(strict=True)
        repository.relative_to(Path(source).resolve(strict=True))
        repositories.append((repository, revision))
    if not any(path == Path(source).resolve(strict=True) and revision == WEBRTC_REV for path, revision in repositories):
        raise ValueError("Actual revisions omit the pinned source root")
    for repository, revision in repositories:
        top = Path(run(["git", "rev-parse", "--show-toplevel"], repository, True, depot).strip()).resolve(strict=True)
        if (top != repository or run(["git", "rev-parse", "HEAD"], repository, True, depot).strip().lower() != revision.lower()):
            raise ValueError("Dependency checkout is not its exact pinned repository")
        run(["git", "-c", "core.fsmonitor=false", "diff", "--no-ext-diff", "--exit-code", "--quiet", "HEAD", "--"], repository, depot=depot)


def verify_copied_sources(source, recipe, hashes):
    for name, expected in hashes.items():
        if (digest(regular_file(source / "luma_audio_factory" / name, 1048576)) != expected
                or digest(regular_file(recipe / name, 1048576)) != expected):
            raise ValueError("Addon source changed during compilation/license generation")


def verify_reused_base(receipt_path, expected_receipt_sha256, base_aar, abi,
                       source_deps, actual_revisions):
    """Authenticate an immutable official base artifact; never manufacture a build."""
    receipt_path = regular_file(receipt_path, 1048576)
    if (not isinstance(expected_receipt_sha256, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_receipt_sha256)
            or digest(receipt_path).upper() != expected_receipt_sha256.upper()):
        raise ValueError("Explicit official base receipt pin mismatch")
    value = json.loads(receipt_path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
    if (type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1
            or value.get("kind") != "official-webrtc-android-build"
            or value.get("webrtcRevision") != WEBRTC_REV or value.get("depotToolsRevision") != DEPOT_REV
            or value.get("abi") != abi or value.get("nativeOrDeviceExecution") is not False
            or value.get("hardwareQualified") is not False):
        raise ValueError("Official base provenance/pin/ABI mismatch")
    root = receipt_path.parent
    manifest = value.get("files")
    aar_name = value.get("aar")
    if (not isinstance(aar_name, str) or PurePosixPath(aar_name).name != aar_name
            or not isinstance(manifest, dict) or not 8 <= len(manifest) <= 256):
        raise ValueError("Official base asset manifest invalid")
    required = {aar_name, "build-args.gn", "DEPS", "source-revisions.txt", "LICENSE.md",
                "licenses/LICENSE", "licenses/PATENTS", "licenses/AUTHORS"}
    if not required.issubset(manifest):
        raise ValueError("Official base asset closure incomplete")
    total = 0
    for name, expected in manifest.items():
        asset = manifest_member(root, name)
        total += asset.stat().st_size
        if (total > 1073741824 or not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)
                or digest(asset).upper() != expected.upper()):
            raise ValueError("Official base asset hash/bounds mismatch")
    recorded_aar = manifest_member(root, aar_name)
    if (Path(base_aar).resolve(strict=True) != recorded_aar.resolve(strict=True)
            or digest(recorded_aar).upper() != str(value.get("aarSha256")).upper()):
        raise ValueError("Supplied AAR is not the authenticated official base")
    if manifest_member(root, "DEPS").read_bytes() != bytes(source_deps):
        raise ValueError("Prepared source DEPS differ from the official base")
    revisions = manifest_member(root, "source-revisions.txt")
    if revision_map(revisions.read_text(encoding="utf-8")) != revision_map(actual_revisions):
        raise ValueError("Prepared dependency revisions differ from the official base")
    wanted = "jni/" + abi + "/" + BASE_LIBRARY
    with zipfile.ZipFile(recorded_aar) as archive:
        entries = archive.infolist()
        if ([entry.filename for entry in entries if entry.filename.startswith("jni/")] != [wanted]
                or len({entry.filename for entry in entries}) != len(entries)):
            raise ValueError("Official base AAR native closure differs")
        with archive.open(wanted) as stream:
            validate_elf_header(stream.read(64), abi)
    return {"arguments": regular_file(manifest_member(root, "build-args.gn"), 1048576).read_bytes(),
            "receiptSha256": digest(receipt_path), "nativeSha256": base_library_hash(recorded_aar, abi),
            "sourceRevisionsSha256": digest(revisions)}


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


def generate_licenses(source, build, output, gn, depot):
    # Keep the official pinned collector and all of its missing-license checks.
    # Only its GN invocation gains the supported root-target switch. The pinned
    # source file itself is never edited.
    generator = regular_file(source / "tools_webrtc/libs/generate_licenses.py", 1048576)
    spec = importlib.util.spec_from_file_location("luma_official_license_collector", generator)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def rooted_gn(buildfile_dir, target):
        return run([gn, "desc", "--all", "--format=json", buildfile_dir, target,
                    "--root-target=" + ROOT_TARGET], source, capture=True, depot=depot)

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
    depot = Path(options.depot_tools).resolve(strict=True)
    base_aar = regular_file(Path(options.base_aar).resolve(strict=True), 536870912)
    reused = bool(options.reused_base)
    if reused != bool(options.base_receipt and options.expected_base_receipt_sha256):
        raise ValueError("Verified artifact reuse requires all explicit receipt arguments")
    if not reused and (options.base_receipt or options.expected_base_receipt_sha256):
        raise ValueError("Receipt arguments require explicit reused-base mode")
    build_dir = Path(options.build_dir).absolute() if reused else Path(options.build_dir).resolve(strict=True)
    if reused:
        if build_dir.exists() or build_dir.is_symlink():
            raise ValueError("Verified-base mode requires a fresh addon build directory")
        build_dir.resolve().relative_to((source / "out").resolve())
        if build_dir.resolve() == (source / "out").resolve():
            raise ValueError("A dedicated fresh addon build directory is required")
    output = Path(options.output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("A fresh addon output directory is required")
    if run(["git", "rev-parse", "HEAD"], source, True).strip() != WEBRTC_REV:
        raise ValueError("WebRTC pin mismatch")
    if run(["git", "rev-parse", "HEAD"], depot, True).strip() != DEPOT_REV:
        raise ValueError("depot_tools pin mismatch")
    gn = tools(source)
    reused_evidence = None
    actual_revisions = None
    if reused:
        actual_revisions = run([sys.executable, regular_file(depot / "gclient.py"), "revinfo", "--actual"], source, True, depot)
        require_clean_checkouts(source, depot, actual_revisions)
        reused_evidence = verify_reused_base(options.base_receipt, options.expected_base_receipt_sha256,
                                           base_aar, options.abi, regular_file(source / "DEPS").read_bytes(), actual_revisions)
        build_dir.mkdir(parents=True)
        (build_dir / "args.gn").write_bytes(reused_evidence["arguments"])
    args_file = regular_file(build_dir / "args.gn", 1048576)
    before_args = digest(args_file)
    base_sha = digest(base_aar)
    library_before = reused_evidence["nativeSha256"] if reused else digest(regular_file(build_dir / BASE_LIBRARY))
    if base_library_hash(base_aar, options.abi) != library_before:
        raise ValueError("Base AAR differs from this prepared build directory")
    source_hashes = source_copy(source, Path(__file__).resolve().parent)
    run([gn, "gen", build_dir, "--root-target=" + ROOT_TARGET, "--fail-on-unused-args"], source, depot=depot)
    records = json.loads(run([gn, "args", build_dir, "--list", "--json",
                              "--root-target=" + ROOT_TARGET], source, True, depot))
    values = validate_args(records, options.abi)
    if digest(args_file) != before_args:
        raise ValueError("Addon generation changed the base compiler/toolchain arguments")
    run([gn, "check", build_dir, ADDON_TARGET, "--root-target=" + ROOT_TARGET], source, depot=depot)
    if values.get("use_siso") == "true":
        runner = regular_file(source / "third_party/siso/cipd" / ("siso.exe" if sys.platform == "win32" else "siso"))
        command = [runner, "ninja", "-C", build_dir, "-j=" + str(options.jobs), "luma_audio_factory:luma_webrtc_audio"]
    else:
        runner = regular_file(source / "third_party/ninja" / ("ninja.exe" if sys.platform == "win32" else "ninja"))
        command = [runner, "-C", build_dir, "-j", str(options.jobs), "luma_audio_factory:luma_webrtc_audio"]
    run(command, source, depot=depot)
    binary = regular_file(build_dir / LIBRARY)
    machine = elf_machine(binary, options.abi)
    readelf = find_readelf(source)
    symbols = run([readelf, "--dyn-syms", "--wide", binary], source, True, depot)
    validate_exports(symbols)
    if (digest(args_file) != before_args or digest(base_aar) != base_sha
            or not reused and digest(build_dir / BASE_LIBRARY) != library_before):
        raise ValueError("The base AAR/toolchain/library changed during addon compilation")
    output.mkdir(parents=True)
    shutil.copyfile(binary, output / LIBRARY)
    shutil.copyfile(args_file, output / "build-args.gn")
    licenses = generate_licenses(source, build_dir, output, gn, depot)
    verify_copied_sources(source, Path(__file__).resolve().parent, source_hashes)
    if reused:
        after_revisions = run([sys.executable, regular_file(depot / "gclient.py"), "revinfo", "--actual"], source, True, depot)
        require_clean_checkouts(source, depot, after_revisions)
        after = verify_reused_base(options.base_receipt, options.expected_base_receipt_sha256,
                                   base_aar, options.abi, regular_file(source / "DEPS").read_bytes(), after_revisions)
        if (after != reused_evidence or revision_map(after_revisions) != revision_map(actual_revisions)
                or (build_dir / BASE_LIBRARY).exists()):
            raise ValueError("Verified base evidence changed or addon unexpectedly rebuilt the base SDK")
    receipt = {"schemaVersion": 1, "kind": "luma-webrtc-android-audio-build",
               "webrtcRevision": WEBRTC_REV, "depotToolsRevision": DEPOT_REV,
               "abi": options.abi, "baseAarSha256": base_sha, "baseBuildArgsSha256": before_args,
               "nativeLibrary": LIBRARY, "nativeSha256": digest(output / LIBRARY),
               "nativeAbiVersion": 1, "sourceSha256": source_hashes,
               "elfMachine": machine, "jniExports": list(EXPORTS), "licenses": licenses}
    if reused:
        receipt.update({"buildMode": "verified-base-artifact",
                        "baseBuildReceiptSha256": reused_evidence["receiptSha256"],
                        "baseNativeSha256": reused_evidence["nativeSha256"],
                        "sourceRevisionsSha256": reused_evidence["sourceRevisionsSha256"]})
    (output / "addon-build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print("LUMA_ANDROID_AUDIO_ADDON_BUILD_PASS; no runtime/media qualification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "build-dir", "depot-tools", "base-aar", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--abi", choices=ABIS, required=True)
    parser.add_argument("--jobs", type=int, choices=range(1, 33), default=4)
    parser.add_argument("--reused-base", action="store_true",
                        help="Build addon only against an explicitly pinned, fully verified official base artifact")
    parser.add_argument("--base-receipt")
    parser.add_argument("--expected-base-receipt-sha256")
    build(parser.parse_args())


if __name__ == "__main__":
    main()
