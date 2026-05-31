#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Install the standalone GPBot CLI from GitHub Release artifacts.

Usage:
  curl -fsSL https://.../install.sh | bash

Environment:
  GPBOT_CLI_REPO       GitHub repo that hosts releases (default: d4lvl13n/gpbot-cli)
  GPBOT_CLI_VERSION    Version without prefix, for example 0.1.0 (default: latest)
  GPBOT_CLI_INSTALL_DIR Install directory (default: ~/.local/bin)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

repo="${GPBOT_CLI_REPO:-d4lvl13n/gpbot-cli}"
version="${GPBOT_CLI_VERSION:-latest}"
install_dir="${GPBOT_CLI_INSTALL_DIR:-${HOME}/.local/bin}"
state_dir="${GPBOT_CLI_STATE_DIR:-${HOME}/.local/share/gpbot-cli}"

case "$(uname -s)" in
  Darwin) os="macos" ;;
  Linux) os="linux" ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

case "$(uname -m)" in
  arm64|aarch64) arch="arm64" ;;
  x86_64|amd64) arch="x86_64" ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

if [[ "${os}" == "linux" && "${arch}" != "x86_64" ]]; then
  echo "No Linux ${arch} artifact is currently published." >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi

artifact="gpbot-${os}-${arch}.pyz"
if [[ "${version}" == "latest" ]]; then
  url="https://github.com/${repo}/releases/latest/download/${artifact}"
else
  url="https://github.com/${repo}/releases/download/gpbot-cli-v${version}/${artifact}"
fi

mkdir -p "${install_dir}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT

echo "Downloading ${url}" >&2
if curl -fsSL "${url}" -o "${tmp}"; then
  chmod 0755 "${tmp}"
  mv "${tmp}" "${install_dir}/gpbot.pyz"

  cat > "${install_dir}/gpbot" <<EOF
#!/usr/bin/env bash
exec python3 "${install_dir}/gpbot.pyz" "\$@"
EOF
  chmod 0755 "${install_dir}/gpbot"
  echo "Installed gpbot to ${install_dir}/gpbot" >&2
  echo "Run: gpbot login" >&2
  exit 0
fi

echo "No ${artifact} release asset found; falling back to wheel install." >&2
wheel_url="$(python3 - "${repo}" "${version}" <<'PY'
from __future__ import annotations

import json
import re
import sys
import urllib.request

repo = sys.argv[1]
version = sys.argv[2]
if version == "latest":
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
else:
    api_url = f"https://api.github.com/repos/{repo}/releases/tags/gpbot-cli-v{version}"

with urllib.request.urlopen(api_url) as response:
    release = json.load(response)

for asset in release.get("assets", []):
    name = asset.get("name") or ""
    if re.fullmatch(r"gpbot_cli-[^-]+-py3-none-any\\.whl", name):
        print(asset["browser_download_url"])
        raise SystemExit(0)

raise SystemExit("No gpbot_cli wheel found in release assets")
PY
)"

venv_dir="${state_dir}/venv"
rm -rf "${venv_dir}"
python3 -m venv "${venv_dir}"
"${venv_dir}/bin/python" -m pip install --upgrade pip >/dev/null
"${venv_dir}/bin/python" -m pip install "${wheel_url}"

cat > "${install_dir}/gpbot" <<EOF
#!/usr/bin/env bash
exec "${venv_dir}/bin/gpbot" "\$@"
EOF
chmod 0755 "${install_dir}/gpbot"

echo "Installed gpbot to ${install_dir}/gpbot using ${venv_dir}" >&2
echo "Run: gpbot login" >&2
