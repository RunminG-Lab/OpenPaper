from __future__ import annotations

import base64
import json
import os
import re
from urllib import error, request

from backend.utils import log, safe_rel

from .models import clean_page_text


def normalize_chat_endpoint(base_url):
    url = (base_url or "").strip()
    if not url:
        raise ValueError("未配置 API 地址")
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def _image_to_data_url(rel_path, workspace_root):
    abs_path = safe_rel(rel_path, workspace_root)
    if not abs_path or not rel_path:
        return None
    if not os.path.isfile(abs_path):
        return None
    with open(abs_path, "rb") as file_obj:
        encoded = base64.b64encode(file_obj.read()).decode("ascii")
    mime = "image/jpeg" if rel_path.lower().endswith(".jpg") else "image/png"
    return f"data:{mime};base64,{encoded}"


def _extract_completion_text(payload):
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item.get("text"))
            return "\n".join(parts)
    output = payload.get("output")
    if isinstance(output, list):
        parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content.get("text"))
        if parts:
            return "\n".join(parts)
    if isinstance(payload.get("content"), str):
        return payload.get("content")
    return ""


def _extract_json_block(text):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:
        pass

    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start:index + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break
        start = raw.find("{", start + 1)
    raise ValueError("模型返回中未找到合法 JSON")


def _should_fallback_to_text_only(message, has_image_inputs):
    lowered = (message or "").lower()
    if not has_image_inputs:
        return False
    if any(code in lowered for code in ("http 401", "http 403", "http 404", "http 429")):
        return False
    return any(token in lowered for token in (
        "http 400",
        "http 415",
        "http 422",
        "image",
        "image_url",
        "vision",
        "multimodal",
        "unsupported",
        "content",
        "messages",
        "没有生成文本内容",
    ))


class OpenAICompatibleClient:
    def __init__(self, api_config, workspace_root):
        self.api_config = api_config if isinstance(api_config, dict) else {}
        self.workspace_root = workspace_root
        self.api_key = (self.api_config.get("apiKey") or "").strip()
        self.model = (self.api_config.get("model") or "").strip()
        if not self.api_key:
            raise ValueError("未配置 API Key")
        if not self.model:
            raise ValueError("未配置模型名称")
        self.endpoint = normalize_chat_endpoint(self.api_config.get("apiBaseUrl"))
        try:
            timeout_sec = int(self.api_config.get("timeoutSec") or 180)
        except (TypeError, ValueError):
            timeout_sec = 180
        self.timeout_sec = max(30, min(timeout_sec, 600))
        self._opener = request.build_opener(request.ProxyHandler({}))

    def request_json(self, prompt, candidate_pages, max_tokens=3200):
        content_items = [{"type": "text", "text": prompt}]
        has_image_inputs = False
        for item in candidate_pages or []:
            image_path = item.get("image_path")
            if not image_path:
                continue
            data_url = _image_to_data_url(image_path, self.workspace_root)
            if not data_url:
                continue
            has_image_inputs = True
            content_items.append({
                "type": "text",
                "text": f"候选图表页 {item.get('id')} / page {item.get('page')}，自动标记：{item.get('reason')}。请结合该页图表与前述文本片段交叉解读。",
            })
            content_items.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })

        if has_image_inputs:
            try:
                return self._send_chat_request(content_items, "multimodal", max_tokens)
            except RuntimeError as exc:
                if not _should_fallback_to_text_only(str(exc), has_image_inputs):
                    raise
                log("当前接口可能不支持图像输入或多模态消息，已自动降级为纯文本速读重试")
        return self._send_chat_request(prompt, "text-only", max_tokens)

    def _send_chat_request(self, user_content, request_mode, max_tokens):
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": "你是严格依据论文证据输出 JSON 的论文速读助手。",
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
        }

        req = request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            method="POST",
        )

        log(f"send speed-read request: model={self.model}, mode={request_mode}, endpoint={self.endpoint}, timeout={self.timeout_sec}s")
        try:
            with self._opener.open(req, timeout=self.timeout_sec) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                log(f"收到模型回复，长度 {len(body)}")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
            log(f"HTTP 错误 {exc.code}: {detail[:200]}")
            raise RuntimeError(f"模型接口返回 HTTP {exc.code}: {clean_page_text(detail, 600)}")
        except error.URLError as exc:
            log(f"连接失败: {exc.reason}")
            raise RuntimeError(f"无法连接模型接口: {exc.reason}")
        except Exception as exc:
            log(f"请求异常: {exc}")
            raise RuntimeError(f"请求模型接口异常: {exc}")

        try:
            data = json.loads(body)
            log("JSON 解析成功")
        except json.JSONDecodeError as exc:
            log(f"JSON 解析失败: {exc}, 响应长度 {len(body)} 字符")
            log(f"响应片段: {body[:500]}")
            raise RuntimeError(f"模型接口返回了无效 JSON，可能是错误或网关问题。详情: {clean_page_text(str(exc), 300)}")

        content = _extract_completion_text(data)
        if not content:
            log(f"无法从响应中提取文本。响应结构: {json.dumps(data, ensure_ascii=False)[:500]}")
            raise RuntimeError("模型接口返回成功，但没有生成文本内容")

        log(f"提取生成文本: {len(content)} 字符")
        return _extract_json_block(content), request_mode, self.endpoint