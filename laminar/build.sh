#!/bin/bash
# Build laminard and laminarc (laminar/Containerfile: linked against nothing but glibc) and
# put them in OUT (default ~/laminar/bin, where laminar.service runs them from). A running
# laminard keeps its old binary until it's restarted (systemctl --user restart laminar).
#
#     laminar/build.sh [OUT]
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
out=${1:-$HOME/laminar/bin}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

podman build --pull=missing -f "$here/Containerfile" --output "type=local,dest=$work" "$here"

# Only glibc may be left to load: anything else could go away with a system update
for binary in laminard laminarc; do
    unexpected=$(ldd "$work/$binary" | grep -vE '^\s*(linux-vdso|/lib64/ld-linux|lib(c|m)\.so\.6)' || true)
    if [[ -n $unexpected ]]; then
        echo "error: $binary loads more than glibc:" >&2
        echo "$unexpected" >&2
        exit 1
    fi
done

mkdir -p "$out"
for binary in laminard laminarc; do
    install -m 755 "$work/$binary" "$out/$binary"
done
echo "built laminard and laminarc into $out"
