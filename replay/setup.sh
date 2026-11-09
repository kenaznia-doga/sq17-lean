#!/bin/bash
# Runner setup: free disk, pinned elan, Lean from lean-toolchain, release archives, optional Mathlib cache.
set -euo pipefail
sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /opt/hostedtoolcache /usr/local/share/boost /usr/share/swift /usr/local/.ghcup &
elan_sha=42b94d4244e8353142c456ec0e4ca6528fd898a6c604d4059f494e706e431f63
curl -fsSL --retry 3 https://github.com/leanprover/elan/releases/download/v4.2.4/elan-x86_64-unknown-linux-gnu.tar.gz -o /tmp/elan.tar.gz
echo "$elan_sha  /tmp/elan.tar.gz" | sha256sum --check
tar -xzf /tmp/elan.tar.gz -C /tmp
/tmp/elan-init -y --no-modify-path --default-toolchain none
echo "$HOME/.elan/bin" >> "$GITHUB_PATH"
export PATH="$HOME/.elan/bin:$PATH"
elan toolchain install "$(cat lean-toolchain)"
lean --version
mkdir -p certificates
gh release download v1.0.0 --repo ofitzharding/sq17-lean --dir certificates
if [ "${MATHLIB:-0}" = 1 ]; then
  lake exe cache get
fi
wait
df -h / ; free -g ; nproc
