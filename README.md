# GPBot CLI

Standalone HTTPS-backed GPBot operator CLI.

Customers do not need the private GPBot server source code, Docker, Postgres,
Redis, Celery, or a server checkout.

After install:

```bash
gpbot login
gpbot doctor
gpbot cli
gpbot pool items --json --limit 5
gpbot mcp serve
```

The direct GitHub Release installer is the primary zero-source-code path.
