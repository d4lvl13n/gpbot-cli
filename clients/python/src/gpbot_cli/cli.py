from __future__ import annotations

import argparse
import cmd
import getpass
import json
import os
import shlex
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import requests

try:
    from gpbot_cli import __version__
except Exception:  # pragma: no cover - defensive for unusual launchers
    __version__ = "unknown"

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "gpbot" / "operator-cli.json"
DEFAULT_BASE_URL = "https://app.codolie.com"
USER_AGENT = f"gpbot-cli/{__version__}"
MCP_PROXY_USER_AGENT = f"gpbot-cli-mcp-proxy/{__version__}"

GLOBAL_FLAG_OPTIONS = {"--json", "--insecure"}
GLOBAL_VALUE_OPTIONS = {"--config", "--profile", "--timeout"}

POOL_ENDPOINTS = {
    "schema": "/api/operator/pool/schema",
    "sources": "/api/operator/pool/sources",
    "items": "/api/operator/pool/items",
    "entities": "/api/operator/pool/entities",
    "clusters": "/api/operator/pool/clusters",
    "volume": "/api/operator/pool/volume",
    "press": "/api/operator/pool/press",
    "tenants": "/api/operator/pool/tenants",
}

POOL_DEFAULT_COLUMNS = {
    "schema": ["name", "sort_default", "purpose"],
    "sources": ["source_id", "source", "vertical", "items_7d", "items_30d", "items_all"],
    "items": ["pool_item_id", "source", "vertical", "lane", "value_score", "published_at", "title"],
    "entities": ["entity_id", "canonical_name", "items_7d", "items_30d", "items_all", "tenant_coverage_count_30d"],
    "clusters": ["label", "canonical", "hot", "baseline", "rising", "distinct_sources_hot"],
    "volume": ["type", "window", "date", "items", "distinct_sources"],
    "press": ["press_item_id", "tenant_id", "tenant_name", "lane", "processing_state", "published_at", "title"],
    "tenants": ["tenant_id", "tenant_name", "is_active", "vertical", "press_items_7d", "press_items_all"],
}


class CliError(RuntimeError):
    """User-facing CLI error with clean stderr reporting."""


@dataclass
class OperatorProfile:
    name: str
    base_url: str
    api_key: str
    tenant_id: int | None = None
    tenant_slug: str | None = None
    vertical: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OperatorProfile":
        return cls(
            name=str(payload.get("name") or "").strip(),
            base_url=str(payload.get("base_url") or "").strip(),
            api_key=str(payload.get("api_key") or "").strip(),
            tenant_id=int(payload["tenant_id"]) if payload.get("tenant_id") is not None else None,
            tenant_slug=str(payload.get("tenant_slug") or "").strip() or None,
            vertical=str(payload.get("vertical") or "").strip() or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_display_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        if payload.get("api_key"):
            payload["api_key"] = _mask_secret(str(payload["api_key"]))
        return payload


def _mask_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "****"
    return f"{secret[:6]}...{secret[-4:]}"


class ProfileStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"active_profile": None, "profiles": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CliError(f"Invalid config file '{self.path}': {exc}") from exc
        if not isinstance(payload, dict):
            raise CliError(f"Invalid config file '{self.path}': expected JSON object")
        payload.setdefault("active_profile", None)
        payload.setdefault("profiles", {})
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except Exception:
            pass
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
        tmp.replace(self.path)

    def set_profile(self, profile: OperatorProfile, *, activate: bool = False) -> None:
        payload = self.load()
        profiles = payload.setdefault("profiles", {})
        profiles[profile.name] = profile.to_dict()
        if activate or not payload.get("active_profile"):
            payload["active_profile"] = profile.name
        self.save(payload)

    def remove_profile(self, name: str) -> None:
        payload = self.load()
        profiles = payload.setdefault("profiles", {})
        if name not in profiles:
            raise CliError(f"Profile '{name}' not found")
        del profiles[name]
        if payload.get("active_profile") == name:
            payload["active_profile"] = next(iter(profiles), None)
        self.save(payload)

    def set_active(self, name: str) -> None:
        payload = self.load()
        profiles = payload.setdefault("profiles", {})
        if name not in profiles:
            raise CliError(f"Profile '{name}' not found")
        payload["active_profile"] = name
        self.save(payload)

    def get_profile(self, name: str | None = None) -> OperatorProfile:
        payload = self.load()
        profiles = payload.setdefault("profiles", {})
        target = name or payload.get("active_profile")
        if not target:
            raise CliError("No active profile configured. Run `gpbot login` first.")
        raw = profiles.get(target)
        if not isinstance(raw, dict):
            raise CliError(f"Profile '{target}' not found")
        profile = OperatorProfile.from_dict(raw)
        if not profile.base_url or not profile.api_key:
            raise CliError(f"Profile '{target}' is incomplete")
        return profile

    def list_profiles(self) -> tuple[str | None, list[OperatorProfile]]:
        payload = self.load()
        profiles = []
        for raw in payload.get("profiles", {}).values():
            if isinstance(raw, dict):
                profiles.append(OperatorProfile.from_dict(raw))
        profiles.sort(key=lambda item: item.name.lower())
        return payload.get("active_profile"), profiles


class RemoteOperatorClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        verify_ssl: bool = True,
        user_agent: str = USER_AGENT,
    ):
        if not base_url:
            raise CliError("base_url is required")
        if not api_key:
            raise CliError("api_key is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": user_agent,
            }
        )

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None, files: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                files=files,
                data=data,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise CliError(f"{method} {url} failed: {exc}") from exc

        content_type = response.headers.get("content-type", "")
        payload: Any
        if "application/json" in content_type:
            try:
                payload = response.json()
            except ValueError:
                payload = {"error": response.text.strip() or "Invalid JSON response"}
        else:
            payload = response.text

        if response.status_code >= 400:
            message = None
            if isinstance(payload, dict):
                message = payload.get("error") or payload.get("message")
            if not message and isinstance(payload, str):
                message = payload.strip()
            raise CliError(message or f"HTTP {response.status_code} calling {path}")
        return payload

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post_json(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json_body=payload or {})

    def post_file(self, path: str, *, file_field: str, filename: str, content: str, extra_fields: dict[str, Any] | None = None) -> Any:
        files = {
            file_field: (filename, content.encode("utf-8"), "text/csv"),
        }
        data = {key: "" if value is None else str(value) for key, value in (extra_fields or {}).items()}
        return self._request("POST", path, files=files, data=data)


def _config_path(raw_path: str | None) -> Path:
    env_path = os.getenv("GPBOT_OPERATOR_CLI_CONFIG", "").strip()
    if raw_path:
        return Path(raw_path).expanduser()
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_CONFIG_PATH


def _resolve_profile(args: argparse.Namespace) -> OperatorProfile:
    store = ProfileStore(_config_path(getattr(args, "config", None)))
    if getattr(args, "base_url", None) or getattr(args, "api_key", None):
        profile = OperatorProfile(
            name=getattr(args, "profile", None) or "adhoc",
            base_url=str(getattr(args, "base_url", "") or "").strip(),
            api_key=str(getattr(args, "api_key", "") or "").strip(),
            tenant_id=getattr(args, "tenant_id", None),
            tenant_slug=getattr(args, "tenant_slug", None),
            vertical=getattr(args, "vertical", None),
        )
        if not profile.base_url or not profile.api_key:
            raise CliError("--base-url and --api-key must be provided together for ad-hoc use")
        return profile
    return store.get_profile(getattr(args, "profile", None))


def _build_client(args: argparse.Namespace, *, user_agent: str = USER_AGENT) -> RemoteOperatorClient:
    profile = _resolve_profile(args)
    return RemoteOperatorClient(
        base_url=profile.base_url,
        api_key=profile.api_key,
        timeout=float(getattr(args, "timeout", 30.0)),
        verify_ssl=not bool(getattr(args, "insecure", False)),
        user_agent=user_agent,
    )


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def _iter_field_value(row: dict[str, Any], field: str) -> Any:
    cur: Any = row
    for part in field.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _print_table(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> None:
    if not rows:
        print("(no results)")
        return

    def _cell(row: dict[str, Any], col: str) -> str:
        value = _iter_field_value(row, col)
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        elif value is None:
            text = ""
        else:
            text = str(value)
        text = " ".join(text.split())
        return text[:60] + "..." if len(text) > 63 else text

    widths: dict[str, int] = {}
    for col in columns:
        widths[col] = max(len(col), max(len(_cell(row, col)) for row in rows))

    header = " | ".join(col.ljust(widths[col]) for col in columns)
    divider = "-+-".join("-" * widths[col] for col in columns)
    print(header)
    print(divider)
    for row in rows:
        print(" | ".join(_cell(row, col).ljust(widths[col]) for col in columns))


def _output(payload: Any, *, json_mode: bool, columns: Sequence[str] | None = None, row_key: str | None = None) -> None:
    if json_mode:
        print(_json_dump(payload))
        return
    if row_key and isinstance(payload, dict) and isinstance(payload.get(row_key), list):
        rows = payload.get(row_key) or []
        if columns:
            _print_table(rows, columns)
        else:
            print(_json_dump(payload))
        return
    if isinstance(payload, list) and columns:
        _print_table(payload, columns)
        return
    if isinstance(payload, dict):
        print(_json_dump(payload))
        return
    print(payload)


def _fields_or_default(raw_fields: str | None, default: Sequence[str]) -> list[str]:
    if not raw_fields:
        return list(default)
    parsed = [field.strip() for field in raw_fields.split(",") if field.strip()]
    return parsed or list(default)


def _json_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _compact_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _print_kv(payload: dict[str, Any]) -> None:
    width = max((len(key) for key in payload), default=0)
    for key, value in payload.items():
        print(f"{key.ljust(width)} : {_compact_value(value)}")


def doctor_command(args: argparse.Namespace) -> None:
    profile = _resolve_profile(args)
    client = _build_client(args)
    health_payload = client.get_json("/api/health")
    doctor_payload = client.get_json("/api/operator/doctor")
    payload = {
        "profile": profile.name,
        "baseUrl": profile.base_url,
        "apiHealth": health_payload,
        "operator": doctor_payload,
    }
    if _json_requested(args):
        _output(payload, json_mode=True)
        return
    summary = {
        "profile": profile.name,
        "base_url": profile.base_url,
        "tenant": doctor_payload.get("tenant", {}).get("slug") or doctor_payload.get("tenant", {}).get("name"),
        "tenant_id": doctor_payload.get("tenant", {}).get("id"),
        "auth_type": doctor_payload.get("auth", {}).get("authType"),
        "api_status": health_payload.get("status"),
        "operator_ok": doctor_payload.get("ok"),
    }
    _print_kv(summary)


def _pool_output_columns(args: argparse.Namespace, subcommand: str) -> list[str]:
    return _fields_or_default(getattr(args, "fields", None), POOL_DEFAULT_COLUMNS[subcommand])


def _pool_command(args: argparse.Namespace, subcommand: str, params: dict[str, Any]) -> None:
    client = _build_client(args)
    query_params = {key: value for key, value in params.items() if value is not None}
    payload = client.get_json(POOL_ENDPOINTS[subcommand], params=query_params)
    _output(
        payload,
        json_mode=_json_requested(args),
        columns=_pool_output_columns(args, subcommand),
        row_key="rows",
    )


def pool_schema_command(args: argparse.Namespace) -> None:
    _pool_command(args, "schema", {})


def pool_sources_command(args: argparse.Namespace) -> None:
    _pool_command(
        args,
        "sources",
        {
            "vertical": args.vertical,
            "since": args.since,
            "min_items": args.min_items,
            "sort": args.sort,
            "limit": args.limit,
        },
    )


def pool_items_command(args: argparse.Namespace) -> None:
    _pool_command(
        args,
        "items",
        {
            "vertical": args.vertical,
            "entity": args.entity,
            "source": args.source,
            "source_id": args.source_id,
            "lane": args.lane,
            "cluster": args.cluster,
            "content_kind": args.content_kind,
            "state": args.state,
            "min_score": args.min_score,
            "max_score": args.max_score,
            "since": args.since,
            "until": args.until,
            "window": args.window,
            "tenant_uncovered": args.tenant_uncovered,
            "uncovered_state": args.uncovered_state,
            "no_legacy": args.no_legacy,
            "sort": args.sort,
            "limit": args.limit,
        },
    )


def pool_entities_command(args: argparse.Namespace) -> None:
    _pool_command(
        args,
        "entities",
        {
            "search": args.search,
            "exact": args.exact,
            "min_items": args.min_items,
            "tenant": args.tenant,
            "no_legacy": args.no_legacy,
            "sort": args.sort,
            "limit": args.limit,
        },
    )


def pool_clusters_command(args: argparse.Namespace) -> None:
    _pool_command(
        args,
        "clusters",
        {
            "vertical": args.vertical,
            "entity": args.entity,
            "source": args.source,
            "lane": args.lane,
            "window": args.window,
            "compare": args.compare,
            "min": args.min,
            "limit": args.limit,
        },
    )


def pool_volume_command(args: argparse.Namespace) -> None:
    _pool_command(
        args,
        "volume",
        {
            "vertical": args.vertical,
            "entity": args.entity,
            "source": args.source,
            "lane": args.lane,
            "tenant": args.tenant,
            "no_legacy": args.no_legacy,
            "windows": args.windows,
            "days": args.days,
        },
    )


def pool_press_command(args: argparse.Namespace) -> None:
    _pool_command(
        args,
        "press",
        {
            "tenant": args.tenant,
            "entity": args.entity,
            "no_legacy": args.no_legacy,
            "lane": args.lane,
            "state": args.state,
            "window": args.window,
            "since": args.since,
            "since_updated": args.since_updated,
            "group_by": args.group_by,
            "limit": args.limit,
        },
    )


def pool_tenants_command(args: argparse.Namespace) -> None:
    _pool_command(
        args,
        "tenants",
        {
            "active_only": args.active_only,
            "with_entity": args.with_entity,
            "no_legacy": args.no_legacy,
            "since": args.since,
            "sort": args.sort,
            "limit": args.limit,
        },
    )


def build_mcp_proxy_handlers(client: RemoteOperatorClient) -> dict[str, Callable[..., Any]]:
    def _get(path: str, *, params: dict[str, Any] | None = None) -> Any:
        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        return client.get_json(path, params=clean_params)

    def _post(path: str, payload: dict[str, Any]) -> Any:
        return client.post_json(path, payload)

    def list_topics(page: int = 1, limit: int = 20) -> dict[str, Any]:
        return _get("/api/content-studio/topics", params={"page": page, "limit": limit})

    def submit_topic(
        topic: str,
        lane: str = "Opinion",
        priority: bool = True,
        angle: str = "",
        audience: str = "",
        scope: str = "",
        target_length: str = "",
        title_seed: str = "",
        must_include: list[str] | None = None,
        avoid: list[str] | None = None,
        schedule_mode: str = "immediate",
        start_at: str | None = None,
        publish_at: str | None = None,
    ) -> dict[str, Any]:
        return _post(
            "/api/content-studio/topics",
            {
                "topic": topic,
                "lane": lane,
                "priority": priority,
                "angle": angle,
                "audience": audience,
                "scope": scope,
                "targetLength": target_length,
                "titleSeed": title_seed,
                "mustInclude": must_include or [],
                "avoid": avoid or [],
                "scheduleMode": schedule_mode,
                "startAt": start_at,
                "publishAt": publish_at,
            },
        )

    def upload_topics_csv(csv_content: str, validate_only: bool = False) -> dict[str, Any]:
        return client.post_file(
            "/api/content-studio/topics/upload-csv",
            file_field="file",
            filename="topics.csv",
            content=csv_content,
            extra_fields={"validateOnly": "true" if validate_only else "false"},
        )

    def get_topic(topic_id: int) -> dict[str, Any]:
        return _get(f"/api/content-studio/topics/{topic_id}")

    def topic_action(topic_id: int, action: str) -> dict[str, Any]:
        return _post(f"/api/content-studio/topics/{topic_id}/actions", {"action": action})

    def get_research(topic_id: int) -> dict[str, Any]:
        return _get(f"/api/content-studio/research/{topic_id}")

    def list_scheduled_jobs(page: int = 1, limit: int = 50) -> dict[str, Any]:
        return _get("/api/content-studio/scheduled-jobs", params={"page": page, "limit": limit})

    def scheduled_job_action(job_id: int, action: str, run_at: str | None = None) -> dict[str, Any]:
        body = {"action": action}
        if run_at is not None:
            body["runAt"] = run_at
        return _post(f"/api/content-studio/scheduled-jobs/{job_id}/actions", body)

    def pool_schema() -> dict[str, Any]:
        return _get(POOL_ENDPOINTS["schema"])

    def pool_sources(
        vertical: str | None = None,
        min_items: int = 1,
        sort: str = "volume_desc",
        limit: int = 100,
        since: str | None = None,
    ) -> dict[str, Any]:
        return _get(
            POOL_ENDPOINTS["sources"],
            params={"vertical": vertical, "min_items": min_items, "sort": sort, "limit": limit, "since": since},
        )

    def pool_items(
        vertical: str | None = None,
        source_id: int | None = None,
        source: str | None = None,
        lane: str | None = None,
        content_kind: str | None = None,
        cluster: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        window: int | None = None,
        since: str | None = None,
        until: str | None = None,
        entity: int | None = None,
        state: str | None = None,
        no_legacy: bool = False,
        sort: str = "score_desc",
        limit: int = 50,
        tenant_uncovered: int | None = None,
        uncovered_state: str | None = None,
    ) -> dict[str, Any]:
        return _get(
            POOL_ENDPOINTS["items"],
            params={
                "vertical": vertical,
                "source_id": source_id,
                "source": source,
                "lane": lane,
                "content_kind": content_kind,
                "cluster": cluster,
                "min_score": min_score,
                "max_score": max_score,
                "window": window,
                "since": since,
                "until": until,
                "entity": entity,
                "state": state,
                "no_legacy": no_legacy,
                "sort": sort,
                "limit": limit,
                "tenant_uncovered": tenant_uncovered,
                "uncovered_state": uncovered_state,
            },
        )

    def pool_entities(
        search: str | None = None,
        exact: bool = False,
        min_items: int = 0,
        tenant: int | None = None,
        no_legacy: bool = False,
        sort: str = "name",
        limit: int = 100,
    ) -> dict[str, Any]:
        return _get(
            POOL_ENDPOINTS["entities"],
            params={
                "search": search,
                "exact": exact,
                "min_items": min_items,
                "tenant": tenant,
                "no_legacy": no_legacy,
                "sort": sort,
                "limit": limit,
            },
        )

    def pool_clusters(
        vertical: str | None = None,
        entity: int | None = None,
        source: str | None = None,
        lane: str | None = None,
        window: int = 7,
        compare: int | None = None,
        min: int = 3,
        limit: int = 30,
    ) -> dict[str, Any]:
        return _get(
            POOL_ENDPOINTS["clusters"],
            params={
                "vertical": vertical,
                "entity": entity,
                "source": source,
                "lane": lane,
                "window": window,
                "compare": compare,
                "min": min,
                "limit": limit,
            },
        )

    def pool_volume(
        vertical: str | None = None,
        entity: int | None = None,
        source: str | None = None,
        lane: str | None = None,
        tenant: int | None = None,
        no_legacy: bool = False,
        windows: str | None = None,
        days: int = 14,
    ) -> dict[str, Any]:
        return _get(
            POOL_ENDPOINTS["volume"],
            params={
                "vertical": vertical,
                "entity": entity,
                "source": source,
                "lane": lane,
                "tenant": tenant,
                "no_legacy": no_legacy,
                "windows": windows,
                "days": days,
            },
        )

    def pool_press(
        tenant: int | None = None,
        entity: int | None = None,
        no_legacy: bool = False,
        lane: str | None = None,
        state: str | None = None,
        window: int | None = None,
        since: str | None = None,
        since_updated: str | None = None,
        group_by: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return _get(
            POOL_ENDPOINTS["press"],
            params={
                "tenant": tenant,
                "entity": entity,
                "no_legacy": no_legacy,
                "lane": lane,
                "state": state,
                "window": window,
                "since": since,
                "since_updated": since_updated,
                "group_by": group_by,
                "limit": limit,
            },
        )

    def pool_tenants(
        active_only: bool = False,
        with_entity: int | None = None,
        no_legacy: bool = False,
        since: str = "30d",
        sort: str = "name",
        limit: int = 100,
    ) -> dict[str, Any]:
        return _get(
            POOL_ENDPOINTS["tenants"],
            params={
                "active_only": active_only,
                "with_entity": with_entity,
                "no_legacy": no_legacy,
                "since": since,
                "sort": sort,
                "limit": limit,
            },
        )

    return {
        "list_topics": list_topics,
        "submit_topic": submit_topic,
        "upload_topics_csv": upload_topics_csv,
        "get_topic": get_topic,
        "topic_action": topic_action,
        "get_research": get_research,
        "list_scheduled_jobs": list_scheduled_jobs,
        "scheduled_job_action": scheduled_job_action,
        "pool_schema": pool_schema,
        "pool_sources": pool_sources,
        "pool_items": pool_items,
        "pool_entities": pool_entities,
        "pool_clusters": pool_clusters,
        "pool_volume": pool_volume,
        "pool_press": pool_press,
        "pool_tenants": pool_tenants,
    }


def build_proxy_mcp(client: RemoteOperatorClient):
    from mcp.server.fastmcp import FastMCP

    handlers = build_mcp_proxy_handlers(client)
    mcp = FastMCP(
        "GPBot Operator Proxy",
        instructions=(
            "Thin local MCP proxy for the GPBot operator API. "
            "All tools use the configured local operator profile over HTTPS."
        ),
    )

    @mcp.tool()
    def list_topics(page: int = 1, limit: int = 20) -> dict[str, Any]:
        return handlers["list_topics"](page=page, limit=limit)

    @mcp.tool()
    def submit_topic(
        topic: str,
        lane: str = "Opinion",
        priority: bool = True,
        angle: str = "",
        audience: str = "",
        scope: str = "",
        target_length: str = "",
        title_seed: str = "",
        must_include: list[str] | None = None,
        avoid: list[str] | None = None,
        schedule_mode: str = "immediate",
        start_at: str | None = None,
        publish_at: str | None = None,
    ) -> dict[str, Any]:
        return handlers["submit_topic"](
            topic=topic,
            lane=lane,
            priority=priority,
            angle=angle,
            audience=audience,
            scope=scope,
            target_length=target_length,
            title_seed=title_seed,
            must_include=must_include,
            avoid=avoid,
            schedule_mode=schedule_mode,
            start_at=start_at,
            publish_at=publish_at,
        )

    @mcp.tool()
    def upload_topics_csv(csv_content: str, validate_only: bool = False) -> dict[str, Any]:
        return handlers["upload_topics_csv"](csv_content=csv_content, validate_only=validate_only)

    @mcp.tool()
    def get_topic(topic_id: int) -> dict[str, Any]:
        return handlers["get_topic"](topic_id=topic_id)

    @mcp.tool()
    def topic_action(topic_id: int, action: str) -> dict[str, Any]:
        return handlers["topic_action"](topic_id=topic_id, action=action)

    @mcp.tool()
    def get_research(topic_id: int) -> dict[str, Any]:
        return handlers["get_research"](topic_id=topic_id)

    @mcp.tool()
    def list_scheduled_jobs(page: int = 1, limit: int = 50) -> dict[str, Any]:
        return handlers["list_scheduled_jobs"](page=page, limit=limit)

    @mcp.tool()
    def scheduled_job_action(job_id: int, action: str, run_at: str | None = None) -> dict[str, Any]:
        return handlers["scheduled_job_action"](job_id=job_id, action=action, run_at=run_at)

    @mcp.tool()
    def pool_schema() -> dict[str, Any]:
        return handlers["pool_schema"]()

    @mcp.tool()
    def pool_sources(
        vertical: str | None = None,
        min_items: int = 1,
        sort: str = "volume_desc",
        limit: int = 100,
        since: str | None = None,
    ) -> dict[str, Any]:
        return handlers["pool_sources"](
            vertical=vertical,
            min_items=min_items,
            sort=sort,
            limit=limit,
            since=since,
        )

    @mcp.tool()
    def pool_items(
        vertical: str | None = None,
        source_id: int | None = None,
        source: str | None = None,
        lane: str | None = None,
        content_kind: str | None = None,
        cluster: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        window: int | None = None,
        since: str | None = None,
        until: str | None = None,
        entity: int | None = None,
        state: str | None = None,
        no_legacy: bool = False,
        sort: str = "score_desc",
        limit: int = 50,
        tenant_uncovered: int | None = None,
        uncovered_state: str | None = None,
    ) -> dict[str, Any]:
        return handlers["pool_items"](
            vertical=vertical,
            source_id=source_id,
            source=source,
            lane=lane,
            content_kind=content_kind,
            cluster=cluster,
            min_score=min_score,
            max_score=max_score,
            window=window,
            since=since,
            until=until,
            entity=entity,
            state=state,
            no_legacy=no_legacy,
            sort=sort,
            limit=limit,
            tenant_uncovered=tenant_uncovered,
            uncovered_state=uncovered_state,
        )

    @mcp.tool()
    def pool_entities(
        search: str | None = None,
        exact: bool = False,
        min_items: int = 0,
        tenant: int | None = None,
        no_legacy: bool = False,
        sort: str = "name",
        limit: int = 100,
    ) -> dict[str, Any]:
        return handlers["pool_entities"](
            search=search,
            exact=exact,
            min_items=min_items,
            tenant=tenant,
            no_legacy=no_legacy,
            sort=sort,
            limit=limit,
        )

    @mcp.tool()
    def pool_clusters(
        vertical: str | None = None,
        entity: int | None = None,
        source: str | None = None,
        lane: str | None = None,
        window: int = 7,
        compare: int | None = None,
        min: int = 3,
        limit: int = 30,
    ) -> dict[str, Any]:
        return handlers["pool_clusters"](
            vertical=vertical,
            entity=entity,
            source=source,
            lane=lane,
            window=window,
            compare=compare,
            min=min,
            limit=limit,
        )

    @mcp.tool()
    def pool_volume(
        vertical: str | None = None,
        entity: int | None = None,
        source: str | None = None,
        lane: str | None = None,
        tenant: int | None = None,
        no_legacy: bool = False,
        windows: str | None = None,
        days: int = 14,
    ) -> dict[str, Any]:
        return handlers["pool_volume"](
            vertical=vertical,
            entity=entity,
            source=source,
            lane=lane,
            tenant=tenant,
            no_legacy=no_legacy,
            windows=windows,
            days=days,
        )

    @mcp.tool()
    def pool_press(
        tenant: int | None = None,
        entity: int | None = None,
        no_legacy: bool = False,
        lane: str | None = None,
        state: str | None = None,
        window: int | None = None,
        since: str | None = None,
        since_updated: str | None = None,
        group_by: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return handlers["pool_press"](
            tenant=tenant,
            entity=entity,
            no_legacy=no_legacy,
            lane=lane,
            state=state,
            window=window,
            since=since,
            since_updated=since_updated,
            group_by=group_by,
            limit=limit,
        )

    @mcp.tool()
    def pool_tenants(
        active_only: bool = False,
        with_entity: int | None = None,
        no_legacy: bool = False,
        since: str = "30d",
        sort: str = "name",
        limit: int = 100,
    ) -> dict[str, Any]:
        return handlers["pool_tenants"](
            active_only=active_only,
            with_entity=with_entity,
            no_legacy=no_legacy,
            since=since,
            sort=sort,
            limit=limit,
        )

    return mcp


def mcp_serve_command(args: argparse.Namespace) -> None:
    profile = _resolve_profile(args)
    if args.mode == "direct":
        try:
            from gpbot.mcp.server import mcp as direct_mcp, set_default_tenant_id
        except ImportError as exc:
            raise CliError(
                "Direct MCP mode is server-only. Installed customer CLIs should use proxy mode: "
                "`gpbot mcp serve`."
            ) from exc

        tenant_id = profile.tenant_id
        if tenant_id is None:
            from gpbot.mcp.auth import resolve_tenant_from_api_key

            tenant_id = resolve_tenant_from_api_key(profile.api_key)
        if tenant_id is None:
            raise CliError("Direct MCP mode requires a profile tenant_id or a resolvable tenant API key")
        set_default_tenant_id(int(tenant_id))
        direct_mcp.run(transport=args.transport, mount_path=args.mount_path)
        return

    client = _build_client(args, user_agent=MCP_PROXY_USER_AGENT)
    proxy = build_proxy_mcp(client)
    proxy.run(transport=args.transport, mount_path=args.mount_path)


class OperatorShell(cmd.Cmd):
    intro = "GPBot operator shell. Type help or ? to list commands."

    def __init__(self, args: argparse.Namespace, parser: argparse.ArgumentParser):
        super().__init__()
        self.args = args
        self.parser = parser
        self.json_mode = _json_requested(args)
        self.last_command: list[str] | None = None
        self.profile_override = getattr(args, "profile", None)
        self.adhoc_base_url = getattr(args, "base_url", None)
        self.adhoc_api_key = getattr(args, "api_key", None)
        self._refresh_prompt()

    def _shell_prefix(self) -> list[str]:
        prefix: list[str] = []
        if getattr(self.args, "config", None):
            prefix.extend(["--config", str(self.args.config)])
        if getattr(self.args, "timeout", None) not in (None, 30.0):
            prefix.extend(["--timeout", str(self.args.timeout)])
        if getattr(self.args, "insecure", False):
            prefix.append("--insecure")
        if self.profile_override:
            prefix.extend(["--profile", self.profile_override])
        elif self.adhoc_base_url and self.adhoc_api_key:
            prefix.extend(["--base-url", self.adhoc_base_url, "--api-key", self.adhoc_api_key])
        if self.json_mode:
            prefix.append("--json")
        return prefix

    def _refresh_prompt(self) -> None:
        try:
            profile = _resolve_profile(
                argparse.Namespace(
                    config=getattr(self.args, "config", None),
                    profile=self.profile_override,
                    base_url=self.adhoc_base_url,
                    api_key=self.adhoc_api_key,
                    tenant_id=getattr(self.args, "tenant_id", None),
                    tenant_slug=getattr(self.args, "tenant_slug", None),
                    vertical=getattr(self.args, "vertical", None),
                )
            )
            profile_name = profile.name or "adhoc"
            tenant_slug = profile.tenant_slug or str(profile.tenant_id or "?")
        except Exception:
            profile_name = self.profile_override or "unconfigured"
            tenant_slug = "?"
        self.prompt = f"gpbot:{profile_name}/{tenant_slug} > "

    def _run(self, argv: list[str], *, remember: bool = True) -> int:
        if remember:
            self.last_command = list(argv)
        exit_code = run_cli(self._shell_prefix() + argv, parser=self.parser)
        self._refresh_prompt()
        return exit_code

    def emptyline(self) -> bool:
        return False

    def do_exit(self, _arg: str) -> bool:
        return True

    def do_EOF(self, _arg: str) -> bool:
        print()
        return True

    def do_json(self, arg: str) -> None:
        mode = arg.strip().lower()
        if mode not in {"on", "off"}:
            print("Usage: json on|off")
            return
        self.json_mode = mode == "on"

    def do_last(self, _arg: str) -> None:
        if not self.last_command:
            print("No previous command.")
            return
        self._run(list(self.last_command), remember=False)

    def do_doctor(self, _arg: str) -> None:
        self._run(["doctor"])

    def do_profile(self, arg: str) -> None:
        try:
            argv = ["profile"] + shlex.split(arg)
        except ValueError as exc:
            print(f"Error: {exc}")
            return
        if len(argv) >= 3 and argv[1] == "use":
            exit_code = self._run(argv)
            if exit_code == 0:
                self.profile_override = argv[2]
                self.adhoc_base_url = None
                self.adhoc_api_key = None
                self._refresh_prompt()
            return
        self._run(argv)

    def default(self, line: str) -> None:
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            print(f"Error: {exc}")
            return
        if not argv:
            return
        self._run(argv)


def cli_shell_command(args: argparse.Namespace) -> None:
    shell = OperatorShell(args, build_parser())
    shell.cmdloop()


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_login_api_key(args: argparse.Namespace) -> str:
    if getattr(args, "api_key_stdin", False):
        return sys.stdin.readline().strip()
    if getattr(args, "api_key", None):
        return str(args.api_key).strip()
    env_key = os.getenv("GPBOT_API_KEY", "").strip()
    if env_key:
        return env_key
    return getpass.getpass("GPBot API key: ").strip()


def login_command(args: argparse.Namespace) -> None:
    api_key = _read_login_api_key(args)
    if not api_key:
        raise CliError("API key is required. Run `gpbot login --api-key <key>` or set GPBOT_API_KEY.")

    base_url = (args.base_url or DEFAULT_BASE_URL).rstrip("/")
    client = RemoteOperatorClient(
        base_url=base_url,
        api_key=api_key,
        timeout=float(getattr(args, "timeout", 30.0)),
        verify_ssl=not bool(getattr(args, "insecure", False)),
    )
    doctor_payload = client.get_json("/api/operator/doctor")
    tenant = doctor_payload.get("tenant") if isinstance(doctor_payload, dict) else None
    if not isinstance(tenant, dict):
        raise CliError("Login succeeded but the operator API did not return tenant metadata")

    profile = OperatorProfile(
        name=args.profile or "default",
        base_url=base_url,
        api_key=api_key,
        tenant_id=_optional_int(tenant.get("id")),
        tenant_slug=str(tenant.get("slug") or "").strip() or None,
        vertical=str(tenant.get("vertical") or "").strip() or None,
    )
    store = ProfileStore(_config_path(args.config))
    store.set_profile(profile, activate=True)

    payload = {
        "success": True,
        "activeProfile": profile.name,
        "baseUrl": profile.base_url,
        "tenant": {
            "id": profile.tenant_id,
            "slug": profile.tenant_slug,
            "vertical": profile.vertical,
            "name": tenant.get("name"),
        },
    }
    if args.json:
        _output(payload, json_mode=True)
        return
    print(f"Logged in to {profile.base_url}")
    print(f"Active profile: {profile.name}")
    if profile.tenant_slug or profile.tenant_id:
        print(f"Tenant: {profile.tenant_slug or profile.tenant_id}")


def logout_command(args: argparse.Namespace) -> None:
    store = ProfileStore(_config_path(args.config))
    payload = store.load()
    target = args.profile or payload.get("active_profile")
    if not target:
        raise CliError("No active profile configured")
    store.remove_profile(str(target))
    _output({"success": True, "removed": target, "activeProfile": store.load().get("active_profile")}, json_mode=args.json)


def profile_set_command(args: argparse.Namespace) -> None:
    store = ProfileStore(_config_path(args.config))
    profile = OperatorProfile(
        name=args.name,
        base_url=args.base_url.rstrip("/"),
        api_key=args.api_key.strip(),
        tenant_id=args.tenant_id,
        tenant_slug=args.tenant_slug,
        vertical=args.vertical,
    )
    store.set_profile(profile, activate=args.activate)
    result = {"success": True, "profile": profile.to_display_dict(), "active": store.load().get("active_profile")}
    _output(result, json_mode=args.json)


def profile_list_command(args: argparse.Namespace) -> None:
    store = ProfileStore(_config_path(args.config))
    active, profiles = store.list_profiles()
    rows = []
    for profile in profiles:
        row = profile.to_display_dict()
        row["active"] = profile.name == active
        rows.append(row)
    _output({"activeProfile": active, "profiles": rows}, json_mode=args.json, columns=["active", "name", "tenant_id", "tenant_slug", "vertical", "base_url"], row_key="profiles")


def profile_use_command(args: argparse.Namespace) -> None:
    store = ProfileStore(_config_path(args.config))
    store.set_active(args.name)
    _output({"success": True, "activeProfile": args.name}, json_mode=args.json)


def profile_show_command(args: argparse.Namespace) -> None:
    store = ProfileStore(_config_path(args.config))
    profile = store.get_profile(args.name)
    payload = profile.to_display_dict()
    payload["active"] = store.load().get("active_profile") == profile.name
    _output(payload, json_mode=args.json)


def profile_remove_command(args: argparse.Namespace) -> None:
    store = ProfileStore(_config_path(args.config))
    store.remove_profile(args.name)
    _output({"success": True, "removed": args.name, "activeProfile": store.load().get("active_profile")}, json_mode=args.json)


def topics_list_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json(
        "/api/content-studio/topics",
        params={"page": args.page, "limit": args.limit},
    )
    columns = _fields_or_default(args.fields, ["id", "lane", "stage", "status", "progress", "topic"])
    _output(payload, json_mode=args.json, columns=columns, row_key="topics")


def topics_show_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json(f"/api/content-studio/topics/{args.item_id}")
    _output(payload, json_mode=args.json)


def topics_research_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json(f"/api/content-studio/research/{args.item_id}")
    _output(payload, json_mode=args.json)


def topics_submit_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = {
        "topic": args.topic,
        "lane": args.lane,
        "priority": not args.no_priority,
        "angle": args.angle or "",
        "audience": args.audience or "",
        "scope": args.scope or "",
        "targetLength": args.target_length or "",
        "titleSeed": args.title_seed or "",
        "mustInclude": args.must_include or [],
        "avoid": args.avoid or [],
        "primaryKeyword": args.primary_keyword or "",
        "secondaryKeywords": args.secondary_keywords or [],
        "searchIntent": args.search_intent or "",
        "serpFormat": args.serp_format or "",
        "geoFocus": args.geo_focus or "",
        "faqQuestions": args.faq_questions or [],
        "scheduleMode": args.schedule_mode,
        "startAt": args.start_at,
        "publishAt": args.publish_at,
        "validateOnly": args.validate_only,
    }
    response = client.post_json("/api/content-studio/topics", payload)
    _output(response, json_mode=args.json)


def topics_submit_batch_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    csv_path = Path(args.csv_file).expanduser()
    if not csv_path.exists():
        raise CliError(f"CSV file not found: {csv_path}")
    content = csv_path.read_text(encoding="utf-8")
    payload = client.post_file(
        "/api/content-studio/topics/upload-csv",
        file_field="file",
        filename=csv_path.name,
        content=content,
        extra_fields={"validateOnly": "true" if args.validate_only else "false"},
    )
    _output(payload, json_mode=args.json)


def topics_action_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.post_json(
        f"/api/content-studio/topics/{args.item_id}/actions",
        {"action": args.action},
    )
    _output(payload, json_mode=args.json)


def jobs_list_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json(
        "/api/content-studio/scheduled-jobs",
        params={"page": args.page, "limit": args.limit},
    )
    columns = _fields_or_default(args.fields, ["id", "action", "status", "runAt", "payload.topic"])
    _output(payload, json_mode=args.json, columns=columns, row_key="jobs")


def jobs_action_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    body = {"action": args.action}
    if args.run_at:
        body["runAt"] = args.run_at
    payload = client.post_json(f"/api/content-studio/scheduled-jobs/{args.job_id}/actions", body)
    _output(payload, json_mode=args.json)


def pipeline_status_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json(
        "/api/content-pipeline/overview",
        params={
            "period": args.period,
            "start_date": args.start_date,
            "end_date": args.end_date,
        },
    )
    _output(payload, json_mode=args.json)


def pipeline_items_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    params = {
        "period": args.period,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "stage": args.stage,
        "status": args.status,
        "content_type": args.content_type,
        "derivation_kind": args.derivation_kind,
        "research_collected": args.research_collected,
        "sort_by": args.sort_by,
        "sort_order": args.sort_order,
        "page": args.page,
        "limit": args.limit,
        "search": args.search,
        "source": args.source,
    }
    params = {key: value for key, value in params.items() if value not in (None, "")}
    payload = client.get_json("/api/content-pipeline/items", params=params)
    columns = _fields_or_default(args.fields, ["wp_post_id", "lane", "stage", "status", "title", "simon.decision", "source"])
    _output(payload, json_mode=args.json, columns=columns, row_key="items")


def pipeline_item_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json(f"/api/content-pipeline/items/{args.wp_post_id}")
    _output(payload, json_mode=args.json)


def pipeline_manual_review_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json(
        "/api/content-pipeline/manual-review",
        params={
            "page": args.page,
            "limit": args.limit,
            "sort_by": args.sort_by,
            "sort_order": args.sort_order,
        },
    )
    columns = _fields_or_default(args.fields, ["id", "lane", "title", "error", "updated_at"])
    _output(payload, json_mode=args.json, columns=columns, row_key="items")


def simon_show_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json(f"/api/content-pipeline/items/{args.wp_post_id}")
    if args.json:
        _output(payload.get("simon"), json_mode=True)
        return
    simon_payload = payload.get("simon") or {}
    _output(simon_payload, json_mode=False)


def simon_run_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.post_json(f"/api/content-pipeline/items/{args.wp_post_id}/simon/run", {})
    _output(payload, json_mode=args.json)


def brainidea_health_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json("/api/tenant/brainidea/health")
    _output(payload, json_mode=args.json)


def brainidea_dashboard_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json("/api/tenant/brainidea/dashboard")
    _output(payload, json_mode=args.json)


def brainidea_opportunities_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    json_mode = bool(getattr(args, "json", False))

    # Enqueue mode: dispatch to specific opportunity
    if getattr(args, 'enqueue', None):
        body: dict[str, Any] = {}
        if args.lane:
            body["lane"] = args.lane
        if args.note:
            body["note"] = args.note
        if args.respect_cap:
            body["respect_cap"] = True
        try:
            payload = client._request(
                "POST",
                f"/api/tenant/brainidea/opportunities/{args.enqueue}/enqueue",
                json_body=body,
            )
            status = payload.get("status", "")
            if not json_mode:
                if status == "dispatched":
                    print(f"Opportunity {args.enqueue} dispatched (lane={payload.get('lane', '')})")
                    if payload.get("warning"):
                        print(f"  Warning: {payload['warning']}")
                elif status == "already_dispatched":
                    print(f"Opportunity {args.enqueue} already dispatched (press_item_id={payload.get('press_item_id')})")
                elif status == "cap_reached":
                    print(f"Daily cap reached ({payload.get('today_total', 0)}/{payload.get('daily_limit', 0)}). Remove --respect-cap to override.")
            _output(payload, json_mode=json_mode)
        except Exception as e:
            raise CliError(f"Failed to enqueue opportunity: {e}")
        return

    # List mode
    params = {"limit": args.limit, "status": args.status}
    if getattr(args, "lane", None):
        params["lane"] = args.lane
    if getattr(args, 'anchor', None):
        params["anchor"] = args.anchor
    params = {key: value for key, value in params.items() if value not in (None, "")}
    payload = client.get_json("/api/tenant/brainidea/opportunities", params=params)

    if json_mode:
        print(_json_dump(payload))
        return

    items = payload.get("opportunities") or []
    if args.by_anchor and items:
        # Group by anchor (using working_title as proxy since anchor label not in items)
        groups: dict[str, list[dict]] = {}
        for item in items:
            key = item.get("working_title", "").split(":")[0].strip() or "Other"
            groups.setdefault(key, []).append(item)
        # Sort groups by max editorial_value
        sorted_groups = sorted(groups.items(), key=lambda g: max(i.get("editorial_value", 0) for i in g[1]), reverse=True)
        for anchor, group in sorted_groups:
            print(f"\n{anchor}")
            for item in sorted(group, key=lambda i: i.get("editorial_value", 0), reverse=True):
                ev = item.get("editorial_value", 0)
                lane = item.get("lane", "")
                wt = item.get("working_title", item.get("topic", ""))
                status = item.get("status", "")
                print(f"  [{ev:.1f}] {lane:10s} {status:10s} ({item['id']}) {wt}")
    else:
        columns = _fields_or_default(
            args.fields,
            ["id", "status", "lane", "priority", "working_title", "press_item_id", "expires_at"],
        )
        _output(payload, json_mode=json_mode, columns=columns, row_key="opportunities")


def brainidea_registry_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    params = {"limit": args.limit, "status": args.status}
    if getattr(args, "anchor", None):
        params["anchor"] = args.anchor
    params = {key: value for key, value in params.items() if value not in (None, "")}
    payload = client.get_json("/api/tenant/brainidea/storyline-registry", params=params)
    columns = _fields_or_default(
        args.fields,
        ["id", "status", "canonical_label", "storyline_type", "registry_key", "last_seen_at"],
    )
    _output(payload, json_mode=args.json, columns=columns, row_key="registry")


def brainidea_registry_repair_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    body: dict[str, Any] = {"action": args.action}
    if args.reason:
        body["reason"] = args.reason
    if args.target_registry_id is not None:
        body["target_registry_id"] = args.target_registry_id
    if args.canonical_label:
        body["canonical_label"] = args.canonical_label
    if args.coverage_storyline_ids:
        body["coverage_storyline_ids"] = [
            int(value.strip())
            for value in str(args.coverage_storyline_ids).split(",")
            if value.strip()
        ]
    payload = client.post_json(f"/api/tenant/brainidea/storyline-registry/{args.registry_id}/repair", body)
    _output(payload, json_mode=args.json)


def brainidea_memory_show_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json("/api/tenant/brainidea/editorial-memory")
    _output(payload, json_mode=args.json)


def brainidea_memory_set_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    memory_text = args.text
    if args.file:
        file_path = Path(args.file).expanduser()
        if not file_path.exists():
            raise CliError(f"Memory file not found: {file_path}")
        memory_text = file_path.read_text(encoding="utf-8")
    if memory_text is None:
        raise CliError("Provide --text or --file")
    payload = client._request(
        "PUT",
        "/api/tenant/brainidea/editorial-memory",
        json_body={"memoryMarkdown": memory_text},
    )
    _output(payload, json_mode=args.json)


def brainidea_runs_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json("/api/tenant/brainidea/release-desk/runs", params={"limit": args.limit})
    columns = _fields_or_default(
        args.fields,
        ["id", "status", "model", "candidateCount", "releasedCount", "deferredCount", "createdAt"],
    )
    _output(payload, json_mode=args.json, columns=columns, row_key="runs")


def brainidea_run_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json(f"/api/tenant/brainidea/release-desk/runs/{args.run_id}")
    _output(payload, json_mode=args.json)


def brainidea_diagnostics_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json("/api/tenant/brainidea/diagnostics")
    _output(payload, json_mode=args.json)


def brainidea_fix_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.post_json(
        "/api/tenant/brainidea/diagnostics/fix",
        {"fix_action": args.fix_action, "entity_id": args.entity_id},
    )
    _output(payload, json_mode=args.json)


def brainidea_trigger_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.post_json(
        "/api/tenant/brainidea/trigger",
        {"task": args.task, "force": bool(args.force)},
    )
    _output(payload, json_mode=args.json)


def selector_brain_memory_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json("/api/tenant/brainidea/selector-brain/memory")
    _output(payload, json_mode=args.json)


def selector_brain_runs_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json("/api/tenant/brainidea/selector-brain/runs", params={"limit": args.limit})
    columns = _fields_or_default(args.fields, ["id", "status", "model", "candidateCount", "selectedCount", "createdAt"])
    _output(payload, json_mode=args.json, columns=columns, row_key="runs")


def selector_brain_run_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.get_json(f"/api/tenant/brainidea/selector-brain/runs/{args.run_id}")
    _output(payload, json_mode=args.json)


def ops_alerts_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    params = {
        "status": args.status,
        "severity": args.severity,
        "category": args.category,
        "tenant_id": args.tenant_id,
        "limit": args.limit,
    }
    params = {key: value for key, value in params.items() if value not in (None, "")}
    payload = client.get_json("/api/admin/ops/alerts", params=params)
    columns = _fields_or_default(
        args.fields,
        ["severity", "status", "tenant_id", "category", "title", "last_seen_at", "fingerprint"],
    )
    _output(payload, json_mode=args.json, columns=columns, row_key="alerts")


def ops_ack_command(args: argparse.Namespace) -> None:
    client = _build_client(args)
    payload = client.post_json(
        f"/api/admin/ops/alerts/{args.fingerprint}/ack",
        {"hours": int(args.hours)},
    )
    _output(payload, json_mode=args.json)


def _add_common_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Path to local operator CLI config file")
    parser.add_argument("--profile", help="Profile name to use (defaults to active profile)")
    parser.add_argument("--base-url", help="Override base URL for ad-hoc use")
    parser.add_argument("--api-key", help="Override tenant API key for ad-hoc use")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def _add_subcommand_json_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit JSON output",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gpbot", description="GPBot remote operator CLI")
    _add_common_connection_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="Authenticate with a GPBot API key")
    login_parser.add_argument("--api-key", default=argparse.SUPPRESS, help="GPBot operator API key. If omitted, prompts securely.")
    login_parser.add_argument("--api-key-stdin", action="store_true", help="Read API key from stdin")
    login_parser.add_argument("--base-url", default=argparse.SUPPRESS, help=f"Operator API base URL (default: {DEFAULT_BASE_URL})")
    login_parser.add_argument("--profile", default=argparse.SUPPRESS, help="Local profile name to save (default: default)")
    login_parser.add_argument("--config", default=argparse.SUPPRESS, help="Path to local operator CLI config file")
    login_parser.add_argument("--insecure", action="store_true", default=argparse.SUPPRESS, help="Disable TLS verification")
    login_parser.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="HTTP timeout in seconds")
    login_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON output")
    login_parser.set_defaults(func=login_command)

    logout_parser = subparsers.add_parser("logout", help="Remove the active local GPBot profile")
    logout_parser.add_argument("--profile", default=argparse.SUPPRESS, help="Profile name to remove (defaults to active profile)")
    logout_parser.add_argument("--config", default=argparse.SUPPRESS, help="Path to local operator CLI config file")
    logout_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON output")
    logout_parser.set_defaults(func=logout_command)

    profile_parser = subparsers.add_parser("profile", help="Manage local tenant profiles")
    profile_sub = profile_parser.add_subparsers(dest="profile_command", required=True)

    profile_set = profile_sub.add_parser("set", help="Create or update a local tenant profile")
    profile_set.add_argument("name")
    profile_set.add_argument("--base-url", default=DEFAULT_BASE_URL)
    profile_set.add_argument("--api-key", required=True)
    profile_set.add_argument("--tenant-id", type=int)
    profile_set.add_argument("--tenant-slug")
    profile_set.add_argument("--vertical")
    profile_set.add_argument("--activate", action="store_true")
    profile_set.add_argument("--json", action="store_true")
    profile_set.set_defaults(func=profile_set_command)

    profile_list = profile_sub.add_parser("list", help="List local tenant profiles")
    profile_list.add_argument("--json", action="store_true")
    profile_list.set_defaults(func=profile_list_command)

    profile_use = profile_sub.add_parser("use", help="Set the active local tenant profile")
    profile_use.add_argument("name")
    profile_use.add_argument("--json", action="store_true")
    profile_use.set_defaults(func=profile_use_command)

    profile_show = profile_sub.add_parser("show", help="Show a profile or the active profile")
    profile_show.add_argument("name", nargs="?")
    profile_show.add_argument("--json", action="store_true")
    profile_show.set_defaults(func=profile_show_command)

    profile_remove = profile_sub.add_parser("remove", help="Remove a local tenant profile")
    profile_remove.add_argument("name")
    profile_remove.add_argument("--json", action="store_true")
    profile_remove.set_defaults(func=profile_remove_command)

    doctor_parser = subparsers.add_parser("doctor", help="Verify profile, auth, network, and operator API health")
    _add_subcommand_json_arg(doctor_parser)
    doctor_parser.set_defaults(func=doctor_command)

    cli_parser = subparsers.add_parser("cli", help="Start the interactive operator shell")
    cli_parser.set_defaults(func=cli_shell_command)

    pool_parser = subparsers.add_parser("pool", help="Read shared content-pool data over HTTPS")
    pool_sub = pool_parser.add_subparsers(dest="pool_command", required=True)

    pool_schema = pool_sub.add_parser("schema", help="Show the content-pool schema envelope")
    pool_schema.add_argument("--fields")
    _add_subcommand_json_arg(pool_schema)
    pool_schema.set_defaults(func=pool_schema_command)

    pool_sources = pool_sub.add_parser("sources", help="List content-pool sources")
    pool_sources.add_argument("--vertical")
    pool_sources.add_argument("--since")
    pool_sources.add_argument("--min-items", type=int, default=1)
    pool_sources.add_argument("--sort", default="volume_desc")
    pool_sources.add_argument("--limit", type=int, default=100)
    pool_sources.add_argument("--fields")
    _add_subcommand_json_arg(pool_sources)
    pool_sources.set_defaults(func=pool_sources_command)

    pool_items = pool_sub.add_parser("items", help="List content-pool items")
    pool_items.add_argument("--vertical")
    pool_items.add_argument("--entity", type=int)
    pool_items.add_argument("--source")
    pool_items.add_argument("--source-id", type=int)
    pool_items.add_argument("--lane")
    pool_items.add_argument("--cluster")
    pool_items.add_argument("--content-kind")
    pool_items.add_argument("--state")
    pool_items.add_argument("--min-score", type=float)
    pool_items.add_argument("--max-score", type=float)
    pool_items.add_argument("--since")
    pool_items.add_argument("--until")
    pool_items.add_argument("--window", type=int)
    pool_items.add_argument("--tenant-uncovered", type=int)
    pool_items.add_argument("--uncovered-state")
    pool_items.add_argument("--no-legacy", action="store_true")
    pool_items.add_argument("--sort", default="score_desc")
    pool_items.add_argument("--limit", type=int, default=50)
    pool_items.add_argument("--fields")
    _add_subcommand_json_arg(pool_items)
    pool_items.set_defaults(func=pool_items_command)

    pool_entities = pool_sub.add_parser("entities", help="List content-pool entities")
    pool_entities.add_argument("--search")
    pool_entities.add_argument("--exact", action="store_true")
    pool_entities.add_argument("--min-items", type=int, default=0)
    pool_entities.add_argument("--tenant", type=int)
    pool_entities.add_argument("--no-legacy", action="store_true")
    pool_entities.add_argument("--sort", default="name")
    pool_entities.add_argument("--limit", type=int, default=100)
    pool_entities.add_argument("--fields")
    _add_subcommand_json_arg(pool_entities)
    pool_entities.set_defaults(func=pool_entities_command)

    pool_clusters = pool_sub.add_parser("clusters", help="Show content-pool cluster trends")
    pool_clusters.add_argument("--vertical")
    pool_clusters.add_argument("--entity", type=int)
    pool_clusters.add_argument("--source")
    pool_clusters.add_argument("--lane")
    pool_clusters.add_argument("--window", type=int, default=7)
    pool_clusters.add_argument("--compare", type=int)
    pool_clusters.add_argument("--min", type=int, default=3)
    pool_clusters.add_argument("--limit", type=int, default=30)
    pool_clusters.add_argument("--fields")
    _add_subcommand_json_arg(pool_clusters)
    pool_clusters.set_defaults(func=pool_clusters_command)

    pool_volume = pool_sub.add_parser("volume", help="Show content-pool volume series")
    pool_volume.add_argument("--vertical")
    pool_volume.add_argument("--entity", type=int)
    pool_volume.add_argument("--source")
    pool_volume.add_argument("--lane")
    pool_volume.add_argument("--tenant", type=int)
    pool_volume.add_argument("--no-legacy", action="store_true")
    pool_volume.add_argument("--windows")
    pool_volume.add_argument("--days", type=int, default=14)
    pool_volume.add_argument("--fields")
    _add_subcommand_json_arg(pool_volume)
    pool_volume.set_defaults(func=pool_volume_command)

    pool_press = pool_sub.add_parser("press", help="List press items from the pool")
    pool_press.add_argument("--tenant", type=int)
    pool_press.add_argument("--entity", type=int)
    pool_press.add_argument("--no-legacy", action="store_true")
    pool_press.add_argument("--lane")
    pool_press.add_argument("--state")
    pool_press.add_argument("--window", type=int)
    pool_press.add_argument("--since")
    pool_press.add_argument("--since-updated")
    pool_press.add_argument("--group-by")
    pool_press.add_argument("--limit", type=int, default=200)
    pool_press.add_argument("--fields")
    _add_subcommand_json_arg(pool_press)
    pool_press.set_defaults(func=pool_press_command)

    pool_tenants = pool_sub.add_parser("tenants", help="List tenants with pool coverage metrics")
    pool_tenants.add_argument("--active-only", action="store_true")
    pool_tenants.add_argument("--with-entity", type=int)
    pool_tenants.add_argument("--no-legacy", action="store_true")
    pool_tenants.add_argument("--since", default="30d")
    pool_tenants.add_argument("--sort", default="name")
    pool_tenants.add_argument("--limit", type=int, default=100)
    pool_tenants.add_argument("--fields")
    _add_subcommand_json_arg(pool_tenants)
    pool_tenants.set_defaults(func=pool_tenants_command)

    mcp_parser = subparsers.add_parser("mcp", help="Run the local GPBot MCP server")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_sub.add_parser("serve", help="Start the MCP server")
    mcp_serve.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    mcp_serve.add_argument("--mount-path")
    mcp_serve.add_argument("--mode", choices=["proxy", "direct"], default="proxy", help=argparse.SUPPRESS)
    mcp_serve.set_defaults(func=mcp_serve_command)

    topics_parser = subparsers.add_parser("topics", help="Content Studio topic operations")
    topics_sub = topics_parser.add_subparsers(dest="topics_command", required=True)

    topics_list = topics_sub.add_parser("list", help="List topics for the active tenant")
    topics_list.add_argument("--page", type=int, default=1)
    topics_list.add_argument("--limit", type=int, default=20)
    topics_list.add_argument("--fields")
    topics_list.set_defaults(func=topics_list_command)

    topics_show = topics_sub.add_parser("show", help="Show topic details")
    topics_show.add_argument("item_id", type=int)
    topics_show.set_defaults(func=topics_show_command)

    topics_research = topics_sub.add_parser("research", help="Show raw Perplexity research for a topic")
    topics_research.add_argument("item_id", type=int)
    topics_research.set_defaults(func=topics_research_command)

    topics_submit = topics_sub.add_parser(
        "submit",
        help="Submit one topic (same JSON shape as Content Studio POST /api/content-studio/topics)",
    )
    topics_submit.add_argument("topic")
    topics_submit.add_argument("--lane", default="Opinion")
    topics_submit.add_argument("--no-priority", action="store_true")
    topics_submit.add_argument(
        "--angle",
        help="Editorial angle (also accepts contentGuidance in the API; sanitized server-side)",
    )
    topics_submit.add_argument("--audience")
    topics_submit.add_argument("--scope")
    topics_submit.add_argument("--target-length")
    topics_submit.add_argument("--title-seed")
    topics_submit.add_argument("--must-include", action="append", default=[], metavar="PHRASE")
    topics_submit.add_argument("--avoid", action="append", default=[], metavar="PHRASE")
    topics_submit.add_argument("--primary-keyword", dest="primary_keyword", metavar="KW")
    topics_submit.add_argument(
        "--secondary-keyword",
        action="append",
        default=[],
        dest="secondary_keywords",
        metavar="KW",
        help="Repeat flag for multiple secondary keywords",
    )
    topics_submit.add_argument("--search-intent", dest="search_intent")
    topics_submit.add_argument("--serp-format", dest="serp_format")
    topics_submit.add_argument("--geo-focus", dest="geo_focus")
    topics_submit.add_argument(
        "--faq-question",
        action="append",
        default=[],
        dest="faq_questions",
        metavar="QUESTION",
        help="Repeat flag for multiple FAQ target questions",
    )
    topics_submit.add_argument("--schedule-mode", default="immediate", choices=["immediate", "start_at", "publish_at"])
    topics_submit.add_argument("--start-at")
    topics_submit.add_argument("--publish-at")
    topics_submit.add_argument("--validate-only", action="store_true")
    topics_submit.set_defaults(func=topics_submit_command)

    topics_batch = topics_sub.add_parser("submit-batch", help="Submit topics from a CSV file")
    topics_batch.add_argument("csv_file")
    topics_batch.add_argument("--validate-only", action="store_true")
    topics_batch.set_defaults(func=topics_submit_batch_command)

    topics_action = topics_sub.add_parser("action", help="Trigger a topic action")
    topics_action.add_argument("item_id", type=int)
    topics_action.add_argument("action", choices=["retry_research", "retry_rewrite", "publish_now"])
    topics_action.set_defaults(func=topics_action_command)

    jobs_parser = subparsers.add_parser("jobs", help="Scheduled job operations")
    jobs_sub = jobs_parser.add_subparsers(dest="jobs_command", required=True)

    jobs_list = jobs_sub.add_parser("list", help="List scheduled jobs")
    jobs_list.add_argument("--page", type=int, default=1)
    jobs_list.add_argument("--limit", type=int, default=50)
    jobs_list.add_argument("--fields")
    jobs_list.set_defaults(func=jobs_list_command)

    jobs_cancel = jobs_sub.add_parser("cancel", help="Cancel a scheduled job")
    jobs_cancel.add_argument("job_id", type=int)
    jobs_cancel.set_defaults(func=jobs_action_command, action="cancel", run_at=None)

    jobs_reschedule = jobs_sub.add_parser("reschedule", help="Reschedule a scheduled job")
    jobs_reschedule.add_argument("job_id", type=int)
    jobs_reschedule.add_argument("--run-at", required=True)
    jobs_reschedule.set_defaults(func=jobs_action_command, action="reschedule")

    jobs_run_now = jobs_sub.add_parser("run-now", help="Run a scheduled job now")
    jobs_run_now.add_argument("job_id", type=int)
    jobs_run_now.set_defaults(func=jobs_action_command, action="run_now", run_at=None)

    pipeline_parser = subparsers.add_parser("pipeline", help="Pipeline inspection operations")
    pipeline_sub = pipeline_parser.add_subparsers(dest="pipeline_command", required=True)

    pipeline_status = pipeline_sub.add_parser("status", help="Show pipeline overview")
    pipeline_status.add_argument("--period", default="24h")
    pipeline_status.add_argument("--start-date")
    pipeline_status.add_argument("--end-date")
    pipeline_status.set_defaults(func=pipeline_status_command)

    pipeline_items = pipeline_sub.add_parser("items", help="List content pipeline items")
    pipeline_items.add_argument("--period", default="7d")
    pipeline_items.add_argument("--start-date")
    pipeline_items.add_argument("--end-date")
    pipeline_items.add_argument("--stage")
    pipeline_items.add_argument("--status")
    pipeline_items.add_argument("--content-type")
    pipeline_items.add_argument("--derivation-kind")
    pipeline_items.add_argument("--research-collected")
    pipeline_items.add_argument("--sort-by", default="date")
    pipeline_items.add_argument("--sort-order", default="desc")
    pipeline_items.add_argument("--page", type=int, default=1)
    pipeline_items.add_argument("--limit", type=int, default=20)
    pipeline_items.add_argument("--search", default="")
    pipeline_items.add_argument("--source")
    pipeline_items.add_argument("--fields")
    pipeline_items.set_defaults(func=pipeline_items_command)

    pipeline_item = pipeline_sub.add_parser("item", help="Show one content-pipeline article by WordPress post id")
    pipeline_item.add_argument("wp_post_id", type=int)
    pipeline_item.set_defaults(func=pipeline_item_command)

    pipeline_manual_review = pipeline_sub.add_parser("manual-review", help="List MANUAL_REVIEW items")
    pipeline_manual_review.add_argument("--page", type=int, default=1)
    pipeline_manual_review.add_argument("--limit", type=int, default=50)
    pipeline_manual_review.add_argument("--sort-by", default="updated_at")
    pipeline_manual_review.add_argument("--sort-order", default="desc")
    pipeline_manual_review.add_argument("--fields")
    pipeline_manual_review.set_defaults(func=pipeline_manual_review_command)

    simon_parser = subparsers.add_parser("simon", help="Inspect or trigger Simon reviews")
    simon_sub = simon_parser.add_subparsers(dest="simon_command", required=True)

    simon_show = simon_sub.add_parser("show", help="Show Simon output for a published article")
    simon_show.add_argument("wp_post_id", type=int)
    simon_show.set_defaults(func=simon_show_command)

    simon_run = simon_sub.add_parser("run", help="Queue Simon for a published article")
    simon_run.add_argument("wp_post_id", type=int)
    simon_run.set_defaults(func=simon_run_command)

    brainidea_parser = subparsers.add_parser("brainidea", help="Coverage-first BrainIdea operator commands")
    brainidea_sub = brainidea_parser.add_subparsers(dest="brainidea_command", required=True)

    brainidea_health = brainidea_sub.add_parser("health", help="Show BrainIdea health")
    brainidea_health.set_defaults(func=brainidea_health_command)

    brainidea_dashboard = brainidea_sub.add_parser("dashboard", help="Show coverage-first BrainIdea dashboard payload")
    brainidea_dashboard.set_defaults(func=brainidea_dashboard_command)

    brainidea_opportunities = brainidea_sub.add_parser("opportunities", help="List editorial opportunities")
    brainidea_opportunities.add_argument("--limit", type=int, default=30)
    brainidea_opportunities.add_argument("--status")
    brainidea_opportunities.add_argument("--anchor", help="Filter by anchor/label name substring")
    brainidea_opportunities.add_argument("--by-anchor", action="store_true", help="Group display by anchor")
    brainidea_opportunities.add_argument("--fields")
    brainidea_opportunities.add_argument("--enqueue", type=int, metavar="OPPORTUNITY_ID",
                                          help="Enqueue a specific opportunity ID")
    brainidea_opportunities.add_argument(
        "--lane",
        help="When listing: filter by lane (comma-separated, case-insensitive). When using --enqueue: override lane on dispatch.",
    )
    brainidea_opportunities.add_argument("--note", help="Editorial note attached to the enqueue")
    brainidea_opportunities.add_argument("--respect-cap", action="store_true", help="Enforce daily cap on enqueue")
    brainidea_opportunities.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit JSON output (equivalent to --json before the brainidea subcommand)",
    )
    brainidea_opportunities.set_defaults(func=brainidea_opportunities_command)

    brainidea_registry = brainidea_sub.add_parser("registry", help="List storyline registry rows")
    brainidea_registry.add_argument("--limit", type=int, default=50)
    brainidea_registry.add_argument("--status")
    brainidea_registry.add_argument("--anchor", help="Filter by anchor/label name substring")
    brainidea_registry.add_argument("--fields")
    brainidea_registry.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON output")
    brainidea_registry.set_defaults(func=brainidea_registry_command)

    brainidea_registry_repair = brainidea_sub.add_parser("registry-repair", help="Merge, split, archive, or force-follow-up a registry row")
    brainidea_registry_repair.add_argument("registry_id", type=int)
    brainidea_registry_repair.add_argument("action", choices=["merge", "split", "archive", "mark_duplicate", "force_follow_up"])
    brainidea_registry_repair.add_argument("--target-registry-id", type=int, help="Target registry id for merge")
    brainidea_registry_repair.add_argument("--canonical-label", help="New canonical label for split")
    brainidea_registry_repair.add_argument("--coverage-storyline-ids", help="Comma-separated storyline ids to move during split")
    brainidea_registry_repair.add_argument("--reason")
    brainidea_registry_repair.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON output")
    brainidea_registry_repair.set_defaults(func=brainidea_registry_repair_command)

    brainidea_memory = brainidea_sub.add_parser("memory", help="Show editorial memory")
    brainidea_memory.set_defaults(func=brainidea_memory_show_command)

    brainidea_memory_set = brainidea_sub.add_parser("memory-set", help="Replace editorial memory")
    brainidea_memory_source = brainidea_memory_set.add_mutually_exclusive_group(required=True)
    brainidea_memory_source.add_argument("--text")
    brainidea_memory_source.add_argument("--file")
    brainidea_memory_set.set_defaults(func=brainidea_memory_set_command)

    brainidea_runs = brainidea_sub.add_parser("runs", help="List release desk runs")
    brainidea_runs.add_argument("--limit", type=int, default=20)
    brainidea_runs.add_argument("--fields")
    brainidea_runs.set_defaults(func=brainidea_runs_command)

    brainidea_run = brainidea_sub.add_parser("run", help="Show one release desk run")
    brainidea_run.add_argument("run_id", type=int)
    brainidea_run.set_defaults(func=brainidea_run_command)

    brainidea_diagnostics = brainidea_sub.add_parser("diagnostics", help="Show BrainIdea diagnostics")
    brainidea_diagnostics.set_defaults(func=brainidea_diagnostics_command)

    brainidea_fix = brainidea_sub.add_parser("fix", help="Apply a BrainIdea diagnostic fix")
    brainidea_fix.add_argument("fix_action")
    brainidea_fix.add_argument("entity_id", type=int)
    brainidea_fix.set_defaults(func=brainidea_fix_command)

    brainidea_trigger = brainidea_sub.add_parser("trigger", help="Queue a coverage-first BrainIdea runtime task")
    brainidea_trigger.add_argument(
        "task",
        choices=["coverage_radar", "coverage_compiler", "opportunity_editor", "release_desk"],
    )
    brainidea_trigger.add_argument("--force", action="store_true")
    brainidea_trigger.set_defaults(func=brainidea_trigger_command)

    selector_parser = subparsers.add_parser("selector-brain", help="Selector-brain inspection")
    selector_sub = selector_parser.add_subparsers(dest="selector_command", required=True)

    selector_memory = selector_sub.add_parser("memory", help="Show selector-brain memory/config")
    selector_memory.set_defaults(func=selector_brain_memory_command)

    selector_runs = selector_sub.add_parser("runs", help="List selector-brain runs")
    selector_runs.add_argument("--limit", type=int, default=20)
    selector_runs.add_argument("--fields")
    selector_runs.set_defaults(func=selector_brain_runs_command)

    selector_run = selector_sub.add_parser("run", help="Show selector-brain run detail")
    selector_run.add_argument("run_id", type=int)
    selector_run.set_defaults(func=selector_brain_run_command)

    ops_parser = subparsers.add_parser("ops", help="Cross-tenant ops watchdog commands")
    ops_sub = ops_parser.add_subparsers(dest="ops_command", required=True)

    ops_alerts = ops_sub.add_parser("alerts", help="List ops watchdog alerts")
    ops_alerts.add_argument("--status", default="open", help="open, acked, resolved, comma-list, or all")
    ops_alerts.add_argument("--severity", choices=["critical", "warning", "info"])
    ops_alerts.add_argument("--category")
    ops_alerts.add_argument("--tenant-id", type=int)
    ops_alerts.add_argument("--limit", type=int, default=100)
    ops_alerts.add_argument("--fields")
    ops_alerts.set_defaults(func=ops_alerts_command)

    ops_ack = ops_sub.add_parser("ack", help="Ack/snooze one ops alert by fingerprint")
    ops_ack.add_argument("fingerprint")
    ops_ack.add_argument("--hours", type=int, default=24)
    ops_ack.set_defaults(func=ops_ack_command)

    return parser


def _normalize_global_args(argv: Sequence[str]) -> list[str]:
    prefix: list[str] = []
    rest: list[str] = []
    index = 0
    raw = list(argv)
    while index < len(raw):
        arg = raw[index]
        if arg in GLOBAL_FLAG_OPTIONS:
            prefix.append(arg)
            index += 1
            continue
        if any(arg.startswith(f"{option}=") for option in GLOBAL_VALUE_OPTIONS):
            prefix.append(arg)
            index += 1
            continue
        if arg in GLOBAL_VALUE_OPTIONS and index + 1 < len(raw):
            prefix.extend([arg, raw[index + 1]])
            index += 2
            continue
        rest.append(arg)
        index += 1
    return prefix + rest


def run_cli(argv: Sequence[str] | None = None, *, parser: argparse.ArgumentParser | None = None) -> int:
    parser = parser or build_parser()
    if argv is not None:
        argv = _normalize_global_args(argv)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        func = getattr(args, "func", None)
        if func is None:
            parser.print_help()
            return 1
        func(args)
        return 0
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) == 0:
        argv = ["cli"] if sys.stdin.isatty() else ["--help"]
    return run_cli(argv, parser=build_parser())


if __name__ == "__main__":
    raise SystemExit(main())
