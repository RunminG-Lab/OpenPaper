"""Read model capabilities from the configured provider without persisting keys."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from html.parser import HTMLParser
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit


CACHE_TTL_SECONDS = 600
DEEPSEEK_MODELS_DOC = "https://api-docs.deepseek.com/api/list-models/"
_CACHE = {}
_DOCS_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _validate_config(api_config):
    if not isinstance(api_config, dict):
        raise ValueError("API 配置须为 JSON 对象")
    for field, label in (("apiBaseUrl", "API 地址"), ("apiKey", "API Key"), ("model", "模型名称")):
        value = api_config.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{label}须为字符串")
        if isinstance(value, str) and any(ord(char) < 32 for char in value.strip()):
            raise ValueError(f"{label}不能包含换行或控制字符")
    return api_config


def normalize_models_endpoint(base_url):
    if base_url is not None and not isinstance(base_url, str):
        raise ValueError("API 地址须为字符串")
    url = (base_url or "").strip()
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in ("http", "https") and parsed.hostname
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and not any(char.isspace() or ord(char) < 32 for char in url)
        )
        parsed.port  # Validate a malformed port before sending anything.
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("API 地址须为 HTTP(S) 地址，不能包含账号密码、查询参数或片段")
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if not path:
        path = "/v1"
    if not path.endswith("/models"):
        path += "/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a provider key to an address supplied by a redirect.
        return None


def _read_url(url, api_key="", timeout=25, max_bytes=8 * 1024 * 1024):
    headers = {"Accept": "application/json, text/html", "Accept-Encoding": "identity"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = request.Request(url, headers=headers, method="GET")
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
    except error.HTTPError as exc:
        messages = {
            401: "API Key 未通过认证，请检查当前地址对应的完整 Key",
            403: "上游拒绝访问模型列表，请检查账户权限及网络出口",
            404: "此 API 地址未提供 /models 模型列表，请核对 API 地址或手动填写模型",
            429: "模型列表请求过于频繁，请稍后重试",
        }
        detail = messages.get(exc.code, "无法读取模型列表，请检查接口服务")
        if 300 <= exc.code < 400:
            detail = "模型地址发生重定向，为保护 Key 未自动跟随；请填写最终 API 地址"
        # Upstream bodies can echo credentials, so do not expose or log them.
        raise RuntimeError(f"读取模型列表失败（HTTP {exc.code}）：{detail}") from None
    except (error.URLError, OSError, TimeoutError):
        raise RuntimeError("无法连接模型列表接口，请检查 API 地址和网络连接") from None
    except Exception:
        raise RuntimeError("模型列表响应未完整接收，请稍后重试") from None
    if len(body) > max_bytes:
        raise RuntimeError("模型列表响应过大，无法读取")
    return body.decode("utf-8", errors="replace")


def _positive_int(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _first_int(*values):
    return next((number for value in values if (number := _positive_int(value))), None)


def _normalize_model(raw, source, source_kind="api"):
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].strip():
        return None
    architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    provider = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
    context = _first_int(
        raw.get("context_window"), raw.get("context_length"), raw.get("max_context_length"),
        raw.get("max_context_tokens"), raw.get("max_model_len"), provider.get("context_length"),
    )
    output = _first_int(
        raw.get("max_output_tokens"), raw.get("max_completion_tokens"),
        raw.get("max_output_token_limit"), provider.get("max_completion_tokens"), raw.get("max_tokens"),
    )
    modalities = raw.get("input_modalities", architecture.get("input_modalities"))
    if isinstance(modalities, list):
        modalities = list(dict.fromkeys(
            item.lower().strip() for item in modalities if isinstance(item, str) and item.strip()
        )) or None
    else:
        modalities = None
    vision = "image" in modalities if modalities is not None else None
    if vision is None and isinstance(raw.get("supports_vision"), bool):
        vision = raw["supports_vision"]
    fields = {"context_window": context, "max_output_tokens": output,
              "input_modalities": modalities, "supports_vision": vision}
    return {
        "id": raw["id"].strip(),
        "name": raw.get("name") if isinstance(raw.get("name"), str) and raw["name"].strip() else raw["id"].strip(),
        **fields,
        "sources": [source],
        "field_sources": {
            name: {"url": source, "kind": source_kind} for name, value in fields.items() if value is not None
        },
    }


def _models_from_payload(payload, source, source_kind="api"):
    records = payload
    if isinstance(payload, dict):
        records = payload.get("data", payload.get("models"))
    if not isinstance(records, list):
        raise RuntimeError("接口未返回有效的模型列表（需要 data 或 models 数组）")
    result = {}
    for raw in records:
        model = _normalize_model(raw, source, source_kind)
        if model:
            result.setdefault(model["id"], model)
    return list(result.values())


class _CodeBlocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag == "pre":
            self._current = []
        elif tag == "br" and self._current is not None:
            self._current.append("\n")

    def handle_endtag(self, tag):
        if tag == "pre" and self._current is not None:
            self.blocks.append("".join(self._current))
            self._current = None

    def handle_data(self, data):
        if self._current is not None:
            self._current.append(data)


def _deepseek_doc_models(force=False):
    with _CACHE_LOCK:
        cached = _DOCS_CACHE.get(DEEPSEEK_MODELS_DOC)
        if not force and cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])
    # Fixed official URL; no API Key or provider-controlled URL is used here.
    html = _read_url(DEEPSEEK_MODELS_DOC, timeout=10, max_bytes=2 * 1024 * 1024)
    parser = _CodeBlocks()
    parser.feed(html)
    models = {}
    for block in parser.blocks:
        try:
            normalized = _models_from_payload(json.loads(block), DEEPSEEK_MODELS_DOC, "official-docs")
        except (ValueError, RuntimeError):
            continue
        for model in normalized:
            if model["id"].startswith("deepseek-"):
                models[model["id"]] = model
    with _CACHE_LOCK:
        _DOCS_CACHE[DEEPSEEK_MODELS_DOC] = (time.monotonic(), copy.deepcopy(models))
    return models


def list_models(api_config, force=False):
    config = _validate_config(api_config)
    endpoint = normalize_models_endpoint(config.get("apiBaseUrl"))
    api_key = (config.get("apiKey") or "").strip()
    fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    cache_key = (endpoint, fingerprint)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if not force and cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            result = copy.deepcopy(cached[1])
            result["cached"] = True
            return result
    body = _read_url(endpoint, api_key)
    try:
        payload = json.loads(body)
    except ValueError:
        raise RuntimeError("模型列表接口没有返回 JSON，请检查 API 地址") from None
    models = _models_from_payload(payload, endpoint)
    warnings = []
    if urlsplit(endpoint).hostname == "api.deepseek.com" and any(
        any(model[field] is None for field in ("context_window", "max_output_tokens", "input_modalities"))
        for model in models
    ):
        try:
            documented = _deepseek_doc_models(force)
        except RuntimeError:
            documented = {}
            warnings.append("未能读取 DeepSeek 官方能力文档；仅使用当前接口返回的信息")
        for model in models:
            extra = documented.get(model["id"])
            if not extra:
                continue
            for field in ("context_window", "max_output_tokens", "input_modalities", "supports_vision"):
                if model[field] is None and extra[field] is not None:
                    model[field] = extra[field]
                    model["field_sources"][field] = extra["field_sources"][field]
                    if DEEPSEEK_MODELS_DOC not in model["sources"]:
                        model["sources"].append(DEEPSEEK_MODELS_DOC)
    if any(model["context_window"] is None or model["max_output_tokens"] is None for model in models):
        warnings.append("部分模型未公开上下文或输出上限，未知字段不会根据模型名称猜测")
    result = {"ok": True, "models": models, "endpoint": endpoint, "warnings": warnings, "cached": False}
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic(), copy.deepcopy(result))
    return result


def get_model_capabilities(api_config):
    config = _validate_config(api_config)
    model_id = (config.get("model") or "").strip()
    if not model_id:
        return {}
    return next((model for model in list_models(config)["models"] if model["id"] == model_id), {})
