# GPBot CLI

Standalone HTTPS-backed GPBot operator CLI.

Customers do not need the GPBot source code, Docker, Postgres, Redis, Celery, or a server checkout. The installed `gpbot` command stores an API key locally and talks to the hosted operator API.

## Install

Published package install:

```bash
pipx install gpbot-cli
```

Homebrew distribution should wrap this package through the formula template in
`clients/homebrew/gpbot.rb.template`.

Release artifacts also include platform-specific `gpbot-*.pyz` zipapps for
Homebrew/GitHub Release installs.

Direct installer fallback after release artifacts are public:

```bash
curl -fsSL https://github.com/d4lvl13n/gpbot-cli/releases/latest/download/install.sh | bash
```

npm installer fallback after release artifacts are public:

```bash
npm install -g gpbot-cli
```

Development install from this checkout:

```bash
pipx install /path/to/gpbot/clients/python
```

## Login

```bash
gpbot login
```

Non-interactive:

```bash
gpbot login --api-key "$GPBOT_API_KEY"
```

The default API endpoint is `https://app.codolie.com`.

## Use

```bash
gpbot doctor
gpbot cli
gpbot pool items --json --limit 5
gpbot mcp serve
```
