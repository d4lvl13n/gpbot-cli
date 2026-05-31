# GPBot CLI npm Installer

This package installs the standalone GPBot CLI and exposes the `gpbot` command.

```bash
npm install -g gpbot-cli
gpbot login
```

The package does not contain the GPBot server source. During install it downloads
the platform-specific `gpbot-*.pyz` artifact from the GPBot CLI GitHub Release.
If the current platform does not have a zipapp artifact, it falls back to the
pure Python wheel in the same release.

Environment:

- `GPBOT_CLI_REPO`: release repo, default `d4lvl13n/gpbot-cli`
- `GPBOT_CLI_VERSION`: release version, default is this npm package version
- `GPBOT_CLI_PYZ`: local zipapp path override for development/testing
- `GPBOT_CLI_FORCE_WHEEL=1`: force wheel fallback path for testing
- `GPBOT_CLI_SKIP_DOWNLOAD=1`: skip postinstall download
