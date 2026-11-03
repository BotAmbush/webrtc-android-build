"""Package-chain fixtures only: fake ELF data is never executed or qualified."""
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("compose", ROOT / "tools/build/Compose-WebRtcAndroid.py")
compose = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compose)


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


class Composition(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="luma-compose-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base, self.addon, self.output = (self.root / name for name in ("base", "addon", "output"))
        self.base.mkdir()
        self.addon.mkdir()
        (self.base / "licenses").mkdir()
        (self.addon / "licenses").mkdir()
        self.abi = "arm64-v8a"
        self.native = bytearray(96)
        self.native[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", self.native, 16, 3, 183)
        self.native = bytes(self.native)
        self.addon_native = self.native + b"distinct addon fixture"
        classes = io.BytesIO()
        with zipfile.ZipFile(classes, "w") as jar:
            for name in ("PeerConnection", "PeerConnectionFactory", "DefaultVideoDecoderFactory", "AudioDecoderFactoryFactory", "EglRenderer"):
                jar.writestr("org/webrtc/" + name + ".class", b"fixture only")
        self.aar = self.base / "official.aar"
        with zipfile.ZipFile(self.aar, "w") as aar:
            aar.writestr("AndroidManifest.xml", b"<manifest/>")
            aar.writestr("classes.jar", classes.getvalue())
            aar.writestr("jni/arm64-v8a/" + compose.NATIVE, self.native)
        for name in ("LICENSE", "PATENTS", "AUTHORS"):
            for directory in (self.base, self.addon):
                (directory / "licenses" / name).write_text("Official " + name, encoding="utf-8")
        (self.base / "LICENSE.md").write_text("Base notices", encoding="utf-8")
        (self.addon / "licenses/LICENSE.md").write_text("Combined official notices", encoding="utf-8")
        for directory in (self.base, self.addon):
            (directory / "build-args.gn").write_text('target_os="android"\n', encoding="utf-8")
        for name in ("DEPS", "source-revisions.txt"):
            (self.base / name).write_text("fixture", encoding="utf-8")
        (self.addon / compose.ADDON).write_bytes(self.addon_native)
        pins = {"schemaVersion": 1, "webrtcRevision": compose.WEBRTC,
                "depotToolsRevision": compose.DEPOT, "abi": self.abi}
        self.base_receipt = {**pins, "kind": "official-webrtc-android-build", "aar": self.aar.name,
                             "aarSha256": compose.sha(self.aar.read_bytes()),
                             "files": {p.relative_to(self.base).as_posix(): compose.sha(p.read_bytes())
                                       for p in self.base.rglob("*") if p.is_file()}}
        self.addon_receipt = {**pins, "kind": "luma-webrtc-android-audio-build",
                              "baseAarSha256": self.base_receipt["aarSha256"],
                              "baseBuildArgsSha256": compose.sha((self.base / "build-args.gn").read_bytes()),
                              "nativeLibrary": compose.ADDON, "nativeAbiVersion": 1,
                              "nativeSha256": compose.sha(self.addon_native), "elfMachine": 183,
                              "sourceSha256": {name: compose.sha(name.encode()) for name in
                                               ("BUILD.gn", "stereo_opus_factory.h", "stereo_opus_factory.cc",
                                                "jni_audio_factory.cc", "build_addon.py")},
                              "jniExports": ["Java_com_lumadisplay_app_LumaStereoAudioDecoderFactoryFactory_" + name
                                             for name in ("nativeCreate", "nativeAbiVersion")],
                              "licenses": [{"path": p.relative_to(self.addon).as_posix(), "bytes": p.stat().st_size,
                                            "sha256": compose.sha(p.read_bytes())}
                                           for p in (self.addon / "licenses").iterdir()]}
        self.save()

    def save(self):
        write_json(self.base / "build-receipt.json", self.base_receipt)
        write_json(self.addon / "addon-build-receipt.json", self.addon_receipt)

    def run_composition(self):
        return compose.compose(self.base, self.addon, self.output, self.abi)

    def reject(self):
        with self.assertRaises(ValueError):
            self.run_composition()
        self.assertFalse(self.output.exists(), "Invalid input must not create a qualified output")

    def test_preserves_original_members_and_complete_receipt_chain(self):
        result = self.run_composition()
        with zipfile.ZipFile(self.aar) as base, zipfile.ZipFile(self.output / result["aar"]) as final:
            self.assertEqual(set(final.namelist()), set(base.namelist()) | {"jni/arm64-v8a/" + compose.ADDON})
            for name in base.namelist():
                self.assertEqual(base.read(name), final.read(name))
            self.assertEqual(final.read("jni/arm64-v8a/" + compose.ADDON), self.addon_native)
        actual = compose.receipt(self.output / "build-receipt.json")
        self.assertEqual(actual["kind"], "luma-webrtc-android-stereo-build")
        self.assertFalse(actual["hardwareQualified"])
        self.assertEqual(actual["addonAbi"], 1)
        self.assertEqual(len(actual["nativeLibraries"]), 2)
        for name, expected in actual["files"].items():
            self.assertEqual(compose.sha((self.output / name).read_bytes()), expected)
        self.assertEqual((self.output / "LICENSE.md").read_text(), "Combined official notices")
        self.assertEqual((self.output / "build-args.gn").read_bytes(), (self.base / "build-args.gn").read_bytes())
        self.assertEqual(actual["baseBuildReceiptSha256"], compose.sha((self.base / "build-receipt.json").read_bytes()))

    def test_changed_base_file(self):
        (self.base / "DEPS").write_text("modified")
        self.reject()

    @unittest.skipUnless(os.name == "nt" and (ROOT / "tests/Test-WebRtcAndroidConsumer.ps1").is_file(),
                         "The private Windows APK consumer is a separate local integration check")
    def test_production_powershell_consumer_accepts_composed_output(self):
        result = self.run_composition()
        # Start Windows PowerShell with its own inbox module search path,
        # rather than inheriting PowerShell Core's different module roots.
        environment = {key: value for key, value in os.environ.items() if key.lower() != "psmodulepath"}
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "tests/Test-WebRtcAndroidConsumer.ps1"), "-Aar",
                        str(self.output / result["aar"]), "-ExpectedSha256", result["aarSha256"]], check=True,
                       env=environment)

    def test_wrong_addon_toolchain_arguments(self):
        (self.addon / "build-args.gn").write_text("different")
        self.reject()

    def test_addon_from_another_base(self):
        self.addon_receipt["baseAarSha256"] = "0" * 64
        self.save()
        self.reject()

    def test_wrong_addon_elf_even_if_receipt_rehashed(self):
        wrong = bytearray(self.addon_native)
        struct.pack_into("<H", wrong, 18, 62)
        (self.addon / compose.ADDON).write_bytes(wrong)
        self.addon_receipt["nativeSha256"] = compose.sha(wrong)
        self.save()
        self.reject()

    def test_missing_license_closure(self):
        self.addon_receipt["licenses"] = self.addon_receipt["licenses"][:3]
        self.save()
        self.reject()

    def test_incomplete_jni_exports(self):
        self.addon_receipt["jniExports"] = self.addon_receipt["jniExports"][:1]
        self.save()
        self.reject()

    def test_manifest_traversal(self):
        self.base_receipt["files"]["../outside"] = "0" * 64
        self.save()
        self.reject()

    def test_duplicate_receipt_fields(self):
        path = self.addon / "addon-build-receipt.json"
        path.write_text('{"schemaVersion":1,' + path.read_text()[1:])
        self.reject()

    def test_duplicate_aar_member(self):
        with zipfile.ZipFile(self.aar, "a") as aar:
            aar.writestr("classes.jar", b"duplicate")
        self.base_receipt["aarSha256"] = compose.sha(self.aar.read_bytes())
        self.base_receipt["files"][self.aar.name] = self.base_receipt["aarSha256"]
        self.addon_receipt["baseAarSha256"] = self.base_receipt["aarSha256"]
        self.save()
        self.reject()


if __name__ == "__main__":
    unittest.main(verbosity=2)
