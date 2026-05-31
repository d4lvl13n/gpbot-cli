#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def _read_sha(path: Path) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise SystemExit(f"empty SHA256 file: {path}")
    return raw.split()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the GPBot Homebrew formula from release artifact checksums.")
    parser.add_argument("--version", required=True, help="CLI version, for example 0.1.0")
    parser.add_argument("--repo", required=True, help="GitHub release repo, for example d4lvl13n/gpbot-cli")
    parser.add_argument("--macos-arm64-sha-file", type=Path, required=True)
    parser.add_argument("--macos-x86-64-sha-file", type=Path, required=True)
    parser.add_argument("--template", type=Path, default=Path(__file__).with_name("gpbot.rb.template"))
    parser.add_argument("--output", type=Path, default=Path("gpbot.rb"))
    args = parser.parse_args()

    if "/" not in args.repo:
        raise SystemExit("--repo must look like owner/name")
    owner, repo = args.repo.split("/", 1)

    rendered = args.template.read_text(encoding="utf-8")
    rendered = rendered.replace("VERSION", args.version)
    rendered = rendered.replace("CODOLIE_ORG", owner)
    rendered = rendered.replace("GPBOT_CLI_RELEASE_REPO", repo)
    rendered = rendered.replace("MACOS_ARM64_SHA256", _read_sha(args.macos_arm64_sha_file))
    rendered = rendered.replace("MACOS_X86_64_SHA256", _read_sha(args.macos_x86_64_sha_file))

    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

