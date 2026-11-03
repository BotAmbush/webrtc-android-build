#!/usr/bin/env bash
# Build the official AAR on an isolated Linux build worker, never on a user device.
set -euo pipefail
readonly WEBRTC_REV=19581c3352d3e77b3e9731318279772a05540bbe
readonly DEPOT_REV=914c72296c0a3941d733e01e72ab3911729c2dd8
[[ $# == 3 && $(uname -s) == Linux ]] || { echo 'Usage: Build-WebRtcAndroid.sh WORK OUTPUT arm64-v8a|x86_64 (Linux only)' >&2; exit 2; }
work=$(realpath -m -- "$1")
output=$(realpath -m -- "$2")
abi=$3
[[ $abi == arm64-v8a || $abi == x86_64 ]] || exit 2
[[ $work != / && $output != / && $work != "$output" ]] || exit 2
mkdir -p -- "$work" "$output"
[[ ! -e "$work/.gclient" && ! -e "$work/src" && ! -e "$work/depot_tools" ]] || { echo 'Use a fresh build directory; no cached binaries are accepted.' >&2; exit 2; }
df -h -- "$work"
# The official checkout includes its own pinned Android SDK/NDK and compiler.
export DEPOT_TOOLS_UPDATE=0 DEPOT_TOOLS_METRICS=0 GCLIENT_PY3=1
git clone --filter=blob:none --no-checkout https://chromium.googlesource.com/chromium/tools/depot_tools.git "$work/depot_tools"
git -C "$work/depot_tools" checkout --detach "$DEPOT_REV"
[[ $(git -C "$work/depot_tools" rev-parse HEAD) == "$DEPOT_REV" ]]
export PATH="$work/depot_tools:$PATH"
cd -- "$work"
# A pinned solution avoids first synchronizing an unrelated moving WebRTC HEAD.
cat > .gclient <<EOF
solutions = [{"name": "src", "url": "https://webrtc.googlesource.com/src.git", "managed": False, "custom_deps": {}, "custom_vars": {}}]
target_os = ["android"]
EOF
gclient sync --nohooks --no-history --shallow --revision "src@$WEBRTC_REV"
[[ $(git -C src rev-parse HEAD) == "$WEBRTC_REV" ]]
cd src
# These are the official checked-out prerequisite installer and hooks, not a
# separately downloaded NDK or an opaque third-party Maven AAR.
if [[ -f build/install-build-deps.sh ]]; then
  sudo bash build/install-build-deps.sh --no-prompt --android
elif [[ -f build/install-build-deps.py ]]; then
  sudo python3 build/install-build-deps.py --no-prompt --android
else
  echo 'Pinned Chromium build prerequisites are missing.' >&2; exit 3
fi
gclient runhooks
aar="$output/libwebrtc-$WEBRTC_REV-$abi.aar"
vpython3 tools_webrtc/android/build_aar.py --arch "$abi" --output "$aar" \
  --build-dir "$work/src/out/luma-aar" \
  --extra-gn-args 'rtc_build_examples=false' 'rtc_build_tools=false' 'symbol_level=0' \
  --extra-ninja-switches=-j=2
[[ $(git rev-parse HEAD) == "$WEBRTC_REV" ]]
git diff --exit-code --quiet
gclient revinfo --actual > "$output/source-revisions.txt"
cp -- DEPS "$output/DEPS"
cp -- out/luma-aar/"$abi"/args.gn "$output/build-args.gn"
mkdir -p -- "$output/licenses"
for name in LICENSE PATENTS AUTHORS; do cp -- "$name" "$output/licenses/$name"; done
# build_aar.py generates its complete dependency license text beside the AAR.
[[ -s "$output/LICENSE.md" ]] || { echo 'Official generated dependency licenses are missing.' >&2; exit 4; }
/usr/bin/env WEBRTC_REV="$WEBRTC_REV" DEPOT_REV="$DEPOT_REV" LUMA_ABI="$abi" LUMA_OUTPUT="$output" LUMA_AAR="$aar" python3 - <<'PY'
import datetime, hashlib, io, json, os, pathlib, struct, zipfile
root = pathlib.Path(os.environ['LUMA_OUTPUT'])
aar = pathlib.Path(os.environ['LUMA_AAR'])
abi = os.environ['LUMA_ABI']
with zipfile.ZipFile(aar) as z:
    names = z.namelist()
    required = ['AndroidManifest.xml', 'classes.jar', f'jni/{abi}/libjingle_peerconnection_so.so']
    if any(names.count(n) != 1 for n in required): raise SystemExit('AAR is incomplete or has duplicate required entries')
    if any(n.startswith('jni/') and not n.startswith(f'jni/{abi}/') for n in names): raise SystemExit('Unexpected ABI in single-ABI AAR')
    native = z.read(required[2])
    machine = 183 if abi == 'arm64-v8a' else 62
    if native[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', native, 18)[0] != machine:
        raise SystemExit('Unexpected native ELF class or architecture')
    with zipfile.ZipFile(io.BytesIO(z.read('classes.jar'))) as classes:
        for name in ['PeerConnection', 'PeerConnectionFactory', 'DefaultVideoDecoderFactory', 'EglRenderer']:
            if f'org/webrtc/{name}.class' not in classes.namelist(): raise SystemExit('Missing official Java API')
files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest().upper()
         for p in sorted(root.rglob('*')) if p.is_file() and p.name not in ['build-receipt.json', 'SHA256SUMS']}
receipt = {'schemaVersion': 1, 'kind': 'official-webrtc-android-build',
    'createdUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'webrtcRevision': os.environ['WEBRTC_REV'], 'depotToolsRevision': os.environ['DEPOT_REV'],
    'abi': abi, 'aar': aar.name, 'aarSha256': files[aar.name],
    'sourceUrl': 'https://webrtc.googlesource.com/src', 'repositoryCommit': os.getenv('GITHUB_SHA'),
    'runnerImage': os.getenv('ImageOS'), 'runnerImageVersion': os.getenv('ImageVersion'),
    'nativeOrDeviceExecution': False, 'hardwareQualified': False, 'files': files}
(root/'build-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n', encoding='utf-8')
files['build-receipt.json'] = hashlib.sha256((root/'build-receipt.json').read_bytes()).hexdigest().upper()
(root/'SHA256SUMS').write_text(''.join(f'{sha}  {name}\n' for name,sha in sorted(files.items())), encoding='ascii')
print(json.dumps({'abi':abi, 'aarBytes':aar.stat().st_size, 'aarSha256':files[aar.name]}))
PY
