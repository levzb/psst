#!/bin/sh
# Install psst on macOS or Linux:  curl -fsSL https://raw.githubusercontent.com/levzb/psst/main/install.sh | sh
# PSST_REPO=owner/repo picks the GitHub repository, PSST_DIR the install folder, PSST_FROM a local file to install.
set -eu
repo=${PSST_REPO:-levzb/psst}
say() { printf 'psst: %s\n' "$*" >&2; }

# a folder in your home that is already on PATH, so `psst` works right away; otherwise ~/.local/bin
dir=
for d in "$HOME/.local/bin" "$HOME/bin"; do
  case ":$PATH:" in *":$d:"*) dir=$d; break ;; esac
done
dir=${PSST_DIR:-${dir:-$HOME/.local/bin}}
mkdir -p "$dir"

# the single-file script needs only Python 3.9+; without it, a prebuilt binary
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  asset=psst.py
else
  os=$(uname -s | tr '[:upper:]' '[:lower:]')
  case $(uname -m) in x86_64|amd64) arch=x64 ;; arm64|aarch64) arch=arm64 ;; *) arch=$(uname -m) ;; esac
  if [ "$os" = linux ] && { ldd --version 2>&1 || true; } | grep -qi musl; then os=linux-musl; fi
  asset=psst-$os-$arch
fi

tmp=$(mktemp "$dir/.psst.XXXXXX")
trap 'rm -f "$tmp"' EXIT
if [ -n "${PSST_FROM:-}" ]; then
  cp "$PSST_FROM" "$tmp"
elif command -v curl >/dev/null 2>&1; then
  curl -fsSL "https://github.com/$repo/releases/latest/download/$asset" -o "$tmp" || { say "no $asset in the latest release of $repo; install Python 3.9+ and rerun"; exit 1; }
else
  wget -qO "$tmp" "https://github.com/$repo/releases/latest/download/$asset" || { say "no $asset in the latest release of $repo; install Python 3.9+ and rerun"; exit 1; }
fi
chmod 755 "$tmp"
mv "$tmp" "$dir/psst"
say "installed $dir/psst ($asset)"

# fish autoloads vendor_conf.d without any config edits; other shells are asked before their rc file is touched
if command -v fish >/dev/null 2>&1; then "$dir/psst" --setup fish || true; fi
shell=$(basename "${SHELL:-sh}")
if [ "$shell" != fish ] && (exec </dev/tty) 2>/dev/null; then "$dir/psst" --setup "$shell" </dev/tty || true; fi

case ":$PATH:" in *":$dir:"*) ;; *) say "$dir is not on PATH: until a new terminal loads the integration, run it as $dir/psst" ;; esac
say "next: psst --key  (saves your free OpenRouter key from https://openrouter.ai/keys; psst also asks for it on first use)"
