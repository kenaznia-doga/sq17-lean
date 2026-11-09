#!/bin/bash
# Runner setup: free disk, pinned elan, Lean from lean-toolchain, release archives, optional Mathlib cache.
set -euo pipefail
sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /opt/hostedtoolcache /usr/local/share/boost /usr/share/swift /usr/local/.ghcup &
elan_sha=42b94d4244e8353142c456ec0e4ca6528fd898a6c604d4059f494e706e431f63
retry() { local n=0; until "$@"; do n=$((n+1)); [ $n -ge 8 ] && return 1; echo "retry $n: $*" >&2; sleep $((n*n*5)); done; }
# Authenticated API downloads: anonymous github.com release URLs were returning 504 (GitHub incident, 2026-11-09).
retry gh release download v4.2.4 --repo leanprover/elan -p elan-x86_64-unknown-linux-gnu.tar.gz -O /tmp/elan.tar.gz --clobber
echo "$elan_sha  /tmp/elan.tar.gz" | sha256sum --check
tar -xzf /tmp/elan.tar.gz -C /tmp
/tmp/elan-init -y --no-modify-path --default-toolchain none
echo "$HOME/.elan/bin" >> "$GITHUB_PATH"
export PATH="$HOME/.elan/bin:$PATH"
test "$(cat lean-toolchain)" = "leanprover/lean4:v4.34.1"
retry gh release download v4.34.1 --repo leanprover/lean4 -p lean-4.34.1-linux.tar.zst -O /tmp/lean.tar.zst --clobber
sha256sum /tmp/lean.tar.zst
mkdir -p "$HOME/.elan/toolchains/leanprover--lean4---v4.34.1"
tar --zstd -xf /tmp/lean.tar.zst -C "$HOME/.elan/toolchains/leanprover--lean4---v4.34.1" --strip-components=1
rm /tmp/lean.tar.zst
lean --version
mkdir -p certificates
retry gh release download v1.0.0 --repo ofitzharding/sq17-lean --dir certificates --clobber
if [ "${MATHLIB:-0}" = 1 ]; then
  retry lake exe cache get
fi
wait
df -h / ; free -g ; nproc
