#!/usr/bin/env bash
# Build the oh-isolated-chat managed-backend artifact and pin its digest in the manifest.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="$here/build/stage"
archive="$here/backend/linux-amd64.tar.gz"

rm -rf "$stage"
mkdir -p "$stage/bin"
install -m 0755 "$here/backend/isolated-chat-backend" "$stage/bin/isolated-chat-backend"
# Deterministic archive: the same source must pin the same digest, or the
# committed checksum is unverifiable for anyone who rebuilds it.
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
  -czf "$archive" -C "$stage" bin

digest="$(sha256sum "$archive" | cut -d' ' -f1)"
python3 - "$here/canvas-extension.json" "$digest" <<'PY'
import json
import sys

path, digest = sys.argv[1], sys.argv[2]
with open(path) as handle:
    manifest = json.load(handle)
manifest["backend"]["artifacts"]["linux-amd64"]["sha256"] = digest
with open(path, "w") as handle:
    json.dump(manifest, handle, indent=2)
    handle.write("\n")
PY

echo "built $archive"
echo "sha256 $digest"
