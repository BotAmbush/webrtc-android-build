# Pinned WebRTC Android dependency build

This repository builds an Android library from the official WebRTC source at
`19581c3352d3e77b3e9731318279772a05540bbe`, using depot_tools at
`914c72296c0a3941d733e01e72ab3911729c2dd8`. It also builds a small receive-only
Opus stereo decoder-factory adapter using the same source and Android toolchain.

It contains dependency build recipes and that adapter only. The Luma Display
application, Windows host, display driver, input handling, signing keys, pairing
data and installers are not included. No access to a private application
repository or account secret is required.

Run **Build stereo addon against verified official WebRTC AAR** manually under
Actions. The current retry uses the preserved arm64-v8a official base from
release `webrtc-19581c3352d3-arm64-v8a-41696830014-1`, whose archive and original
receipt are independently pinned in the workflow. It prepares the same source
and toolchain, builds only the addon in a fresh output directory, and verifies
the complete base again before composition. It does not relabel the reused
base as a newly compiled SDK. Preparation omits only the exact pinned root hook
that downloads test audio/video files under `src/resources`; the supported
gclient override and its scope are checked and recorded. Source, dependency
revisions, compiler and runtime-library inputs remain unchanged. The full SDK
build recipe retains its original hooks.

The standard `ubuntu-24.04` GitHub-hosted runner supplies the Linux
build environment; users do not need Linux or WSL on their Windows computer.
No large or paid runner is selected. The job has a 50-minute limit. Dependency
outputs are published as public prerelease assets, with no Actions cache or
Actions artifact storage. GitHub's current standard public-repository runner
billing policy applies.

The release archive contains separate official-base, addon and composed-stereo outputs.
The composed AAR preserves every official base member byte-for-byte and adds
`libluma_webrtc_audio.so` beside the matching official native library. Build
receipts bind the exact upstream pins, ABI, GN arguments, original AAR, native
libraries, adapter source hashes and generated dependency license closure.
Composition validation runs before producing the final receipt. Partial files
retained after a failed build are not a verified dependency.

The adapter uses WebRTC's supported `AudioDecoderFactoryFactory` extension and
returns the same owned-reference type as the builtin factory. It requests real
48kHz stereo Opus with a 10ms packet preference, FEC enabled and DTX disabled;
it creates no microphone or playback device. Dependency compilation does not
qualify JNI loading, Android playback, end-to-end latency or hardware behavior.

Upstream WebRTC, libopus and all other dependencies retain their respective
licenses and patent notices, collected in the build artifact. This repository
does not change their license terms or include the private application's code.
