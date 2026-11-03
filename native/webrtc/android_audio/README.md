# Android Opus stereo receiver factory

This addon uses the supported `AudioDecoderFactoryFactory` extension in WebRTC
`19581c3352d3e77b3e9731318279772a05540bbe`. Build it with the **same pinned source,
NDK, libc++ ABI and GN configuration as the Android AAR**. Its returned pointer is
an owned `webrtc::AudioDecoderFactory` reference, adopted by the SDK exactly as
with `BuiltinAudioDecoderFactoryFactory`. Keep the addon loaded until process
exit; never unload it while a factory, decoder or joined retirement is pending.

The receive-only factory advertises Opus stereo/48k with a 10ms packet preference,
FEC support, DTX disabled and a 128kbit/s maximum average bitrate. It delegates to
the pinned builtin decoder and refuses absent/mono `stereo` configuration. No
SDP text is changed. It creates no audio device, local track or microphone source.

Copy this directory to `src/luma_audio_factory` in the pinned checkout. For the
CPU test only, also copy `native/webrtc/windows/opus_factory.h` byte-for-byte to
`src/luma_audio_factory/host_opus_factory.h` and record its SHA256. Then use:

```
gn gen out/LumaAudio --root-target=//luma_audio_factory --args='<exact platform/toolchain args>'
ninja -C out/LumaAudio luma_audio_factory:luma_webrtc_audio
```

The shared-library target exists only on Android. The opt-in portable CPU target is
`luma_audio_factory:luma_stereo_opus_tests`; it checks actual different-channel
synthetic PCM, encoded stereo packet flags/480-sample duration and decoded
channel separation without audio output. CPU results do not qualify JNI loading,
Android playback, network transport or subjective listening quality.
Use `Test-Cpu.ps1 -SourceRoot <existing-pinned-checkout>` on Windows; this enables
`luma_audio_cpu_tests=true` and uses only already prepared compiler tools.

`build_addon.py` consumes an existing successful official Android build. It
never fetches sources/tools, dispatches jobs, changes GN compiler arguments,
loads a native library or modifies the original AAR. Example invocation:

```
python3 build_addon.py --source-root <checkout/src> --depot-tools <checkout/depot_tools> --build-dir <existing-android-build/arm64-v8a> --base-aar <official-base.aar> --abi arm64-v8a --output <fresh-addon-output> --jobs 4
```

If an official base build was already preserved, its verified artifact can be
reused without claiming that the SDK was rebuilt in the new checkout:

```
python3 build_addon.py --source-root <fresh-pinned-checkout/src> --depot-tools <checkout/depot_tools> --build-dir <checkout/src/out/fresh-audio-arm64> --base-aar <base-output/official-base.aar> --abi arm64-v8a --output <fresh-addon-output> --jobs 2 --reused-base --base-receipt <base-output/build-receipt.json> --expected-base-receipt-sha256 <explicit-receipt-pin>
```

This mode requires the complete immutable official output, including the AAR,
receipt, exact `build-args.gn`, `DEPS`, source revisions and license files. It
checks the receipt pin and every asset, compares the fresh checkout's actual
dependency revisions/DEPS, refuses staged or unstaged tracked changes in the
source/depot/dependency Git repositories, and copies only the verified GN arguments into a
fresh addon output directory. It never plants a preserved SDK library into the
new build output. The addon is compiled from source; its receipt records
`buildMode: verified-base-artifact` and the original receipt/native/revision
hashes. Source/tool preparation remains the caller's separately reviewed step.
The five copied addon inputs and original recipe inputs are rehashed after
compilation and license generation, before the output receipt is written.
Every child build tool receives the verified depot directory first in its own
PATH, including gclient's nested CIPD invocation when preparation ran in a
previous shell step. This does not alter the caller's or machine's environment.
Captured tool failures retain their bounded stdout/stderr in the build log.

`test_gn_contract.py --source-root <existing-pinned-checkout> --output <fresh-fixture-dir>`
exercises the exact pinned Android JNI template bodies using existing local GN.
It verifies missing-`java_targets` refusal and the explicit empty-list fix with
JNI multiplexing enabled and disabled. Its minimal fixture does not generate a
full Android toolchain, compile code or execute Ninja.

The output contains the standalone addon, byte-identical `build-args.gn`,
`addon-build-receipt.json` and official combined-target license notices. A
separate package composer must keep the original base AAR/receipt unchanged and
combine both exact-ABI libraries with checked receipts; this script does not
silently rewrite or publish an AAR.

Package `libluma_webrtc_audio.so` under the same `jni/<abi>/` directory as the
matching `libjingle_peerconnection_so.so`. Record the addon ABI1, source/pin/GN
hashes, ELF ABI and output SHA256, and include both targets in the official GN
license-notice generation. WebRTC BSD/PATENTS and libopus notices remain required.
