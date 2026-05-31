# Homebrew Distribution

The Homebrew formula should live in the public tap, for example
`codolie/homebrew-tap/Formula/gpbot.rb`.

Release flow:

1. Build and publish the `gpbot-cli` Python package.
2. Build the macOS zipapp artifacts through the `Release GPBot CLI` workflow.
3. Create or update the formula in the tap from `gpbot.rb.template` with the
   release version, release repository, and macOS artifact SHA256 values.
4. Run `brew audit --strict gpbot` and `brew test gpbot`.

Render the formula from downloaded release checksum files:

```bash
clients/homebrew/render_formula.py \
  --version 0.1.3 \
  --repo d4lvl13n/gpbot-cli \
  --macos-arm64-sha-file ./gpbot-macos-arm64.pyz.sha256 \
  --linux-x86-64-sha-file ./gpbot-linux-x86_64.pyz.sha256 \
  --output ./Formula/gpbot.rb
```

Do not point the public formula at the private GPBot source repo. The formula
must install only standalone release artifacts.

The first Homebrew template targets Apple Silicon macOS and x86_64 Linux.
Intel macOS and Linux ARM users should use the direct installer or `pipx` path,
which falls back to the pure Python wheel.
