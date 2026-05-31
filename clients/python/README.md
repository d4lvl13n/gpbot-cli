# GPBot CLI

Standalone HTTPS-backed GPBot operator CLI.

Customers do not need the GPBot source code, Docker, Postgres, Redis, Celery, or a server checkout. The installed `gpbot` command stores an API key locally and talks to the hosted operator API.

## Install

Direct GitHub Release installer:

```bash
curl -fsSL https://github.com/d4lvl13n/gpbot-cli/releases/latest/download/install.sh | bash
```

PyPI package install after publishing is enabled:

```bash
pipx install gpbot-cli
```

Homebrew distribution wraps the release zipapp through the formula template in
`clients/homebrew/gpbot.rb.template`:

```bash
brew install d4lvl13n/tap/gpbot
```

npm installer after npm publishing is enabled:

```bash
npm install -g gpbot-cli
```

Release artifacts also include platform-specific `gpbot-*.pyz` zipapps and a
pure Python wheel fallback. None of these artifacts include the GPBot server
source tree.

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
