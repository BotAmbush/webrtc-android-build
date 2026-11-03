#!/usr/bin/env bash
# Prepare pinned source/tools, optionally build the official AAR on an isolated
# Linux build worker. This script is never run on a user device.
set -euo pipefail
readonly WEBRTC_REV=19581c3352d3e77b3e9731318279772a05540bbe
readonly DEPOT_REV=914c72296c0a3941d733e01e72ab3911729c2dd8
prepare_only=false
if [[ $# == 4 && $1 == --prepare-source-only ]]; then prepare_only=true; shift; fi
[[ $# == 3 && $(uname -s) == Linux ]] || { echo 'Usage: Build-WebRtcAndroid.sh [--prepare-source-only] WORK OUTPUT arm64-v8a|x86_64 (Linux only)' >&2; exit 2; }
script_path=$(realpath -- "${BASH_SOURCE[0]}")
readonly script_path
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
if [[ "$prepare_only" == true ]]; then
  # The pinned root has exactly one unnamed hook: downloading test movies and
  # recordings. Suppress only that root hook through gclient's supported
  # custom_hooks contract; keep every named/root and dependency hook intact.
  python3 - "$WEBRTC_REV" <<'PY'
import ast, pathlib, subprocess, sys
root = pathlib.Path.cwd()
deps = (root/'DEPS').read_bytes()
if deps != subprocess.check_output(['git', 'show', sys.argv[1]+':DEPS']):
    raise SystemExit('Source preparation requires the exact pinned DEPS bytes')
tree = ast.parse(deps.decode('utf-8'))
assignments = [node for node in tree.body if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == 'hooks' for target in node.targets)]
if len(assignments) != 1 or not isinstance(assignments[0].value, ast.List) or any(
        isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == 'hooks' for node in tree.body):
    raise SystemExit('Pinned root hook structure changed')
unnamed = []
for hook in assignments[0].value.elts:
    if not isinstance(hook, ast.Dict): raise SystemExit('Invalid pinned root hook')
    fields = {ast.literal_eval(key): value for key, value in zip(hook.keys, hook.values)}
    name = ast.literal_eval(fields['name']) if 'name' in fields else ''
    if name == '': unnamed.append(fields)
expected = ['download_from_google_storage', '--directory', '--recursive', '--num_threads=10',
    '--quiet', '--bucket', 'chromium-webrtc-resources', 'src/resources']
if (len(unnamed) != 1 or set(unnamed[0]) != {'pattern', 'action'}
        or ast.literal_eval(unnamed[0]['pattern']) != '.' or ast.literal_eval(unnamed[0]['action']) != expected):
    raise SystemExit('Only the exact unnamed test-media hook may be omitted')
config = root.parent/'.gclient'
if config.is_symlink() or not config.is_file(): raise SystemExit('Exact fresh gclient configuration required')
text = config.read_text(encoding='utf-8')
if 'custom_hooks' in text: raise SystemExit('Unexpected preexisting hook override')
config.write_text(text+"\nsolutions[0]['custom_hooks'] = [{'name': ''}]\n", encoding='utf-8')
print('LUMA_ANDROID_SOURCE_PREPARATION_OMITS_ONLY_ROOT_TEST_MEDIA')
PY
fi
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
if [[ "$prepare_only" == true ]]; then
  [[ $(git rev-parse HEAD) == "$WEBRTC_REV" ]]
  git diff --no-ext-diff --exit-code --quiet HEAD --
  gclient revinfo --actual > "$output/source-revisions.txt"
  cp -- DEPS "$output/DEPS"
  /usr/bin/env WEBRTC_REV="$WEBRTC_REV" DEPOT_REV="$DEPOT_REV" LUMA_ABI="$abi" LUMA_OUTPUT="$output" LUMA_RECIPE="$script_path" python3 - <<'PY'
import hashlib, json, os, pathlib
root = pathlib.Path(os.environ['LUMA_OUTPUT'])
files = {name: hashlib.sha256((root/name).read_bytes()).hexdigest().upper()
         for name in ('DEPS', 'source-revisions.txt')}
value = {'schemaVersion': 1, 'kind': 'official-webrtc-android-source-preparation',
    'webrtcRevision': os.environ['WEBRTC_REV'], 'depotToolsRevision': os.environ['DEPOT_REV'],
    'abi': os.environ['LUMA_ABI'], 'officialSdkCompiled': False, 'addonCompiled': False,
    'nativeOrDeviceExecution': False,
    'omittedRootTestMediaHook': {'name': '', 'resourcePath': 'src/resources', 'scope': 'root-solution-only'},
    'recipeSha256': hashlib.sha256(pathlib.Path(os.environ['LUMA_RECIPE']).read_bytes()).hexdigest().upper(),
    'files': files}
(root/'source-preparation-receipt.json').write_text(json.dumps(value, indent=2)+'\n', encoding='utf-8')
print('LUMA_ANDROID_SOURCE_PREPARATION_PASS; official SDK compile skipped')
PY
  exit 0
fi
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
