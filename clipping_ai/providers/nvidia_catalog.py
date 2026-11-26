"""NVIDIA NIM catalog discovery with conservative free-endpoint classification."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from clipping_ai.schemas import TEXT_TASKS

logger = logging.getLogger(__name__)

CATALOG_URL = "https://api.ngc.nvidia.com/v2/search/catalog/resources/ENDPOINT"
MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
CATALOG_SOURCE = "https://build.nvidia.com/models"
CACHE_FILENAME = "nvidia-catalog.json"
CACHE_VERSION = 1
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
_PAGE_SIZE = 200
_MAX_PAGES = 20
_TEXT_LABELS = {"chat", "text-to-text", "large language models"}


class NvidiaCatalogError(RuntimeError):
    """Discovery failed without changing the last known-good cache."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _labels(resource: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    labels = resource.get("labels")
    if not isinstance(labels, list):
        return result
    for label in labels:
        if not isinstance(label, dict):
            continue
        key = label.get("key")
        values = label.get("values")
        if isinstance(key, str) and isinstance(values, list):
            result[key] = [str(value) for value in values]
    return result


def _attributes(resource: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    attributes = resource.get("attributes")
    if not isinstance(attributes, list):
        return result
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        key = attribute.get("key")
        value = attribute.get("value")
        if isinstance(key, str) and value is not None:
            result[key] = str(value)
    return result


def _publisher(resource: dict[str, Any], labels: dict[str, list[str]]) -> str:
    values = labels.get("publisher") or []
    return values[0] if values else ""


def _is_text_capable(labels: dict[str, list[str]]) -> bool:
    playground = {value.lower() for value in labels.get("playgroundType", [])}
    general = {value.lower() for value in labels.get("general", [])}
    return "chat" in playground or bool(general & _TEXT_LABELS)


def _catalog_query(page: int) -> dict[str, Any]:
    return {
        "query": "*:*",
        "page": page,
        "pageSize": _PAGE_SIZE,
        "scoredSize": _PAGE_SIZE,
        "groupBy": "resourceType",
        "filters": [
            {"field": "orgName", "value": "qc69jvmznzxy"},
            {"field": "resourceType", "value": "endpoint"},
        ],
        "orderBy": [
            {"field": "score", "value": "DESC"},
            {"field": "nameSort", "value": "ASC"},
            {"field": "resourceId", "value": "ASC"},
            {"field": "resourceType", "value": "ASC"},
        ],
    }


def _resources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    groups = payload.get("results")
    if not isinstance(groups, list):
        return resources
    for group in groups:
        if not isinstance(group, dict):
            continue
        items = group.get("resources")
        if isinstance(items, list):
            resources.extend(item for item in items if isinstance(item, dict))
    return resources


def fetch_catalog(
    *,
    http_get: Callable[..., Any] | None = None,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Fetch every public NVIDIA endpoint resource from the catalog search API."""
    get = http_get or httpx.get
    by_id: dict[str, dict[str, Any]] = {}
    total = 0
    for page in range(_MAX_PAGES):
        query = json.dumps(_catalog_query(page), separators=(",", ":"))
        try:
            response = get(
                CATALOG_URL,
                params={"q": query, "group-labels-by-labelset": "true"},
                timeout=timeout,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - surfaced as discovery failure
            raise NvidiaCatalogError(f"Could not fetch NVIDIA catalog: {exc}") from exc
        if not isinstance(payload, dict):
            raise NvidiaCatalogError("NVIDIA catalog returned an unexpected response shape.")
        total = int(payload.get("resultTotal") or 0)
        before = len(by_id)
        for resource in _resources(payload):
            resource_id = resource.get("resourceId")
            if isinstance(resource_id, str) and resource_id:
                by_id.setdefault(resource_id, resource)
        if len(by_id) >= total or len(by_id) == before:
            break
    if not by_id:
        raise NvidiaCatalogError("NVIDIA catalog returned no endpoint resources.")
    return list(by_id.values())


def fetch_api_models(
    *,
    http_get: Callable[..., Any] | None = None,
    timeout: float = 20.0,
) -> list[str]:
    """Return the public ``/v1/models`` model IDs.

    Presence in this list is only evidence that NVIDIA advertises the model. It
    is not treated as a successful inference verification.
    """
    get = http_get or httpx.get
    try:
        response = get(MODELS_URL, timeout=timeout, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - surfaced as discovery failure
        raise NvidiaCatalogError(f"Could not fetch NVIDIA model list: {exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise NvidiaCatalogError("NVIDIA model list returned an unexpected response shape.")
    return [
        item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def match_api_model(resource: dict[str, Any], api_ids: Iterable[str]) -> str | None:
    """Match a catalog resource to an API model ID without guessing ambiguously."""
    labels = _labels(resource)
    publisher = _publisher(resource, labels)
    names = {
        _normalize(str(resource.get("name") or "")),
        _normalize(str(resource.get("displayName") or "")),
    }
    names.discard("")
    matches = []
    for api_id in api_ids:
        owner, _, model = api_id.partition("/")
        if _normalize(model) not in names:
            continue
        if publisher and _normalize(owner) != _normalize(publisher):
            continue
        matches.append(api_id)
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


def _model_id(publisher: str, provider_model_id: str) -> str:
    source = provider_model_id or publisher
    return f"nvidia-{_slug(source)}"


def classify_resource(
    resource: dict[str, Any],
    api_ids: Iterable[str],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Convert a catalog resource into a registry model when it is a free endpoint."""
    labels = _labels(resource)
    nim_types = {value.lower() for value in labels.get("nimType", [])}
    if "free endpoint" not in nim_types:
        return None

    api_models = list(api_ids)
    publisher = _publisher(resource, labels)
    api_model_id = match_api_model(resource, api_models)
    raw_name = str(resource.get("name") or "").strip()
    guessed_id = f"{publisher}/{raw_name}" if publisher and raw_name else raw_name
    text_capable = _is_text_capable(labels)
    tasks = list(TEXT_TASKS) if text_capable else []
    attributes = _attributes(resource)
    available_value = attributes.get("AVAILABLE")
    catalog_available = (
        available_value.strip().lower() == "true" if available_value is not None else None
    )
    timestamp = (now or _utc_now()).isoformat()

    if api_model_id is None:
        api_available: bool | str = False
        verification_status = "unavailable"
        message = "Not listed in NVIDIA's public /v1/models catalog."
    elif catalog_available is False:
        api_available = False
        verification_status = "unavailable"
        message = "NVIDIA's catalog marks this endpoint as unavailable."
    else:
        api_available = "unknown"
        verification_status = "unverified"
        message = "Listed by NVIDIA; callable status has not been smoke-tested."

    provider_model_id = api_model_id or guessed_id
    return {
        "id": _model_id(publisher, provider_model_id),
        "provider": "nvidia",
        "provider_model_id": provider_model_id,
        "display_name": str(resource.get("displayName") or raw_name or provider_model_id),
        "tasks": tasks,
        "task_capabilities": tasks,
        "free": True,
        "free_verified": False,
        "verified_at": None,
        "verification_source": CATALOG_SOURCE,
        "access_type": "free_endpoint",
        "pricing_status": "free",
        "api_available": api_available,
        "verification_status": verification_status,
        "verification_message": message,
        "context_window": None,
        "max_output_tokens": None,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "supports_json": bool(tasks),
        "supports_word_timestamps": False,
        "daily_request_limit": 0,
        "status": "enabled" if tasks else "disabled",
        "notes": str(resource.get("description") or ""),
        "discovered": True,
        "catalog_source": CATALOG_URL,
        "catalog_resource_id": str(resource.get("resourceId") or ""),
        "catalog_score": float(resource.get("weightPopular") or 0.0),
        "catalog_updated_at": timestamp,
    }


def classify_catalog(
    resources: Iterable[dict[str, Any]],
    api_ids: Iterable[str],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    ids = list(api_ids)
    models = [
        model
        for resource in resources
        if (model := classify_resource(resource, ids, now=now)) is not None
    ]
    models.sort(key=lambda model: (-model["catalog_score"], model["id"]))
    return models


def cache_path(config_dir: Path) -> Path:
    return Path(config_dir) / "local" / CACHE_FILENAME


def load_cache(config_dir: Path) -> dict[str, Any] | None:
    path = cache_path(config_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring invalid NVIDIA catalog cache: %s", exc)
        return None
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return None
    if not isinstance(payload.get("models"), list):
        return None
    return payload


def cached_model_dicts(config_dir: Path) -> list[dict[str, Any]]:
    payload = load_cache(config_dir)
    if payload is None:
        return []
    return [item for item in payload["models"] if isinstance(item, dict)]


def _is_fresh(payload: dict[str, Any], ttl_seconds: int, now: datetime) -> bool:
    updated = payload.get("updated_at")
    if not isinstance(updated, str):
        return False
    try:
        stamp = datetime.fromisoformat(updated)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return now - stamp < timedelta(seconds=ttl_seconds)


def discover(
    config_dir: Path,
    *,
    force: bool = False,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    http_get: Callable[..., Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Refresh the NVIDIA free-endpoint cache from structured catalog metadata."""
    current = now or _utc_now()
    cached = load_cache(config_dir)
    if cached is not None and not force and _is_fresh(cached, ttl_seconds, current):
        return {
            "ok": True,
            "cached": True,
            "updated_at": cached.get("updated_at"),
            "models": len(cached["models"]),
            "stats": cached.get("stats") or {},
        }

    resources = fetch_catalog(http_get=http_get)
    api_ids = fetch_api_models(http_get=http_get)
    models = classify_catalog(resources, api_ids, now=current)
    free_resources = [
        resource
        for resource in resources
        if "free endpoint" in {str(value).lower() for value in _labels(resource).get("nimType", [])}
    ]
    stats = {
        "catalog_resources": len(resources),
        "free_endpoints": len(free_resources),
        "text_capable": sum(1 for model in models if model["tasks"]),
        "api_matched": sum(1 for model in models if model["api_available"] is not False),
        "api_unavailable": sum(1 for model in models if model["api_available"] is False),
    }
    payload = {
        "version": CACHE_VERSION,
        "provider": "nvidia",
        "updated_at": current.isoformat(),
        "catalog_source": CATALOG_SOURCE,
        "catalog_url": CATALOG_URL,
        "models_url": MODELS_URL,
        "stats": stats,
        "models": models,
    }
    path = cache_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return {
        "ok": True,
        "cached": False,
        "updated_at": payload["updated_at"],
        "models": len(models),
        "stats": stats,
    }
