# GPBot CLI npm Installer

This package installs the standalone GPBot CLI zipapp and exposes the `gpbot`
command.

```bash
npm install -g gpbot-cli
gpbot login
```

The package does not contain the GPBot server source. During install it downloads
the platform-specific `gpbot-*.pyz` artifact from the GPBot CLI GitHub Release.

Environment:

- `GPBOT_CLI_REPO`: release repo, default `d4lvl13n/gpbot-cli`
- `GPBOT_CLI_VERSION`: release version, default is this npm package version
- `GPBOT_CLI_PYZ`: local zipapp path override for development/testing
- `GPBOT_CLI_SKIP_DOWNLOAD=1`: skip postinstall download

