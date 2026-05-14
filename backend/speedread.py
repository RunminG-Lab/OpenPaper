"""AI-powered paper speed-read pipeline for OpenPaper.

Extracts text from PDFs, selects representative pages, renders them as
images, and sends them to an OpenAI-compatible API for structured
speed-read generation. Includes automatic fallback from multimodal to
text-only mode when the model doesn't support image inputs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import namedtuple
from datetime import datetime
from urllib.parse import unquote
from urllib import request, error

from backend.utils import log, safe_rel
from backend.metadata import load_metadata, atomic_write_metadata

_Result = namedtuple("_Result", ["status", "payload"])


# ---------------------------------------------------------------------------
# text / content helpers
# ---------------------------------------------------------------------------

def _clean_page_text(text, limit=None):
    cleaned = (text or "").replace("\x00", "")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    if limit and len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + " ..."
    return cleaned


def _speedread_text_value(value, fallback="论文中未明确说明"):
    if not isinstance(value, str):
        return fallback
    cleaned = _clean_page_text(value)
    return cleaned or fallback


def _speedread_list_value(value, fallback_text="论文中未明确说明", max_items=6):
    if not isinstance(value, list):
        return [fallback_text]
    items = []
    for item in value:
        text = _speedread_text_value(item, "")
        if text:
            items.append(text)
        if len(items) >= max_items:
            break
    return items or [fallback_text]


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def _resolve_pdf_entry(file_key, metadata, workspace_root):
    entry = metadata.get(file_key)
    if not isinstance(entry, dict):
        return None, None, None
    pdf_local = entry.get("pdf_local") or entry.get("pdf") or ""
    rel_disk = unquote(pdf_local)
    abs_pdf = safe_rel(rel_disk, workspace_root)
    if not abs_pdf or not os.path.isfile(abs_pdf):
        return entry, rel_disk, None
    return entry, rel_disk.replace("\\", "/"), abs_pdf


def _run_capture(cmd):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _read_pdf_page_count(abs_pdf):
    result = _run_capture(["pdfinfo", abs_pdf])
    if result.returncode != 0:
        return 0
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.IGNORECASE | re.MULTILINE)
    return int(match.group(1)) if match else 0


def _extract_pdf_pages_text(abs_pdf, page_count):
    result = _run_capture(["pdftotext", "-layout", "-enc", "UTF-8", abs_pdf, "-"])
    if result.returncode != 0:
        detail = _clean_page_text(result.stderr or result.stdout, 500)
        raise RuntimeError(f"pdftotext 失败: {detail or '未知错误'}")
    raw_pages = result.stdout.split("\f")
    pages = [_clean_page_text(page) for page in raw_pages]
    while pages and not pages[-1]:
        pages.pop()
    if page_count and len(pages) < page_count:
        pages.extend([""] * (page_count - len(pages)))
    return pages


# ---------------------------------------------------------------------------
# page selection
# ---------------------------------------------------------------------------

def _score_page_for_keywords(page_no, page_count, text, keywords):
    lower = (text or "").lower()
    score = 0
    for keyword in keywords:
        if keyword in lower:
            score += 2
    if "figure" in lower or "fig." in lower or "fig " in lower:
        score += 2
    if "table" in lower:
        score += 2
    if page_no <= 2:
        score += 1
    if page_count and page_no >= max(page_count - 1, 1) and ("conclusion" in lower or "discussion" in lower):
        score += 1
    return score


def _select_speedread_pages(page_texts, page_count, max_image_pages):
    if not page_count:
        page_count = len(page_texts)
    if page_count <= 0:
        return []

    role_rules = [
        ("方法总览图", ["figure", "fig.", "fig ", "framework", "pipeline", "architecture", "overview", "method"]),
        ("核心方法页", ["method", "approach", "module", "algorithm", "framework", "pipeline"]),
        ("实验结果页", ["table", "result", "results", "experiment", "evaluation", "benchmark", "comparison"]),
        ("分析或消融页", ["ablation", "analysis", "case study", "limitation", "discussion"]),
    ]
    page_payloads = []
    for idx in range(page_count):
        text = page_texts[idx] if idx < len(page_texts) else ""
        page_payloads.append((idx + 1, text, (text or "").lower()))

    used = set()
    picked = []
    for label, keywords in role_rules:
        best = None
        for page_no, text, lower in page_payloads:
            if page_no in used or not lower.strip():
                continue
            current_score = _score_page_for_keywords(page_no, page_count, text, keywords)
            if current_score <= 0:
                continue
            if best is None or current_score > best[0]:
                best = (current_score, page_no, text)
        if best is not None:
            used.add(best[1])
            picked.append({
                "page": best[1],
                "reason": label,
                "excerpt": _clean_page_text(best[2], 900),
            })

    if len(picked) < max_image_pages:
        ranked = []
        fallback_keywords = [
            "figure", "fig.", "fig ", "table", "method", "approach", "experiment",
            "evaluation", "benchmark", "ablation", "analysis", "results",
        ]
        for page_no, text, lower in page_payloads:
            if page_no in used:
                continue
            current_score = _score_page_for_keywords(page_no, page_count, text, fallback_keywords)
            if page_no == 1:
                current_score += 2
            ranked.append((current_score, page_no, text))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        for score, page_no, text in ranked:
            if page_no in used:
                continue
            used.add(page_no)
            picked.append({
                "page": page_no,
                "reason": "关键页面" if score > 0 else "补充页面",
                "excerpt": _clean_page_text(text, 900),
            })
            if len(picked) >= max_image_pages:
                break

    if not picked:
        for page_no in sorted({1, min(2, page_count), min(4, page_count)}):
            if not page_no:
                continue
            text = page_texts[page_no - 1] if page_no - 1 < len(page_texts) else ""
            picked.append({
                "page": page_no,
                "reason": "默认候选页",
                "excerpt": _clean_page_text(text, 900),
            })

    picked.sort(key=lambda item: item["page"])
    return picked[:max_image_pages]


# ---------------------------------------------------------------------------
# page rendering
# ---------------------------------------------------------------------------

def _render_speedread_page_image(abs_pdf, file_key, page_no, workspace_root, speedread_cache_dir, image_width):
    paper_hash = hashlib.sha1(file_key.encode("utf-8")).hexdigest()[:16]
    cache_dir = os.path.join(workspace_root, speedread_cache_dir, paper_hash)
    os.makedirs(cache_dir, exist_ok=True)

    base_name = f"page_{page_no:03d}"
    target_jpg = os.path.join(cache_dir, base_name + ".jpg")
    target_png = os.path.join(cache_dir, base_name + ".png")
    prefix = os.path.join(cache_dir, base_name)

    cmd = [
        "pdftoppm",
        "-f", str(page_no),
        "-l", str(page_no),
        "-singlefile",
        "-scale-to", str(image_width),
        "-jpeg",
        abs_pdf,
        prefix,
    ]
    result = _run_capture(cmd)
    if result.returncode != 0 or not os.path.exists(target_jpg):
        fallback = [
            "pdftoppm",
            "-f", str(page_no),
            "-l", str(page_no),
            "-singlefile",
            "-scale-to", str(image_width),
            "-png",
            abs_pdf,
            prefix,
        ]
        result = _run_capture(fallback)
        if result.returncode != 0 or not os.path.exists(target_png):
            detail = _clean_page_text(result.stderr or result.stdout, 300)
            raise RuntimeError(f"渲染第 {page_no} 页失败: {detail or '未知错误'}")
        return os.path.relpath(target_png, workspace_root).replace(os.sep, "/")
    return os.path.relpath(target_jpg, workspace_root).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# grounding text builder
# ---------------------------------------------------------------------------

def _build_speedread_grounding_text(page_texts, candidate_pages, max_source_chars):
    page_count = len(page_texts)
    chosen = set()
    for page_no in [1, 2, 3, page_count - 1, page_count]:
        if 1 <= page_no <= page_count:
            chosen.add(page_no)
    for item in candidate_pages:
        page_no = int(item.get("page") or 0)
        if 1 <= page_no <= page_count:
            chosen.add(page_no)

    key_terms = ["abstract", "introduction", "method", "approach", "experiment", "evaluation", "conclusion", "limitation"]
    for term in key_terms:
        for idx, text in enumerate(page_texts, start=1):
            if term in (text or "").lower():
                chosen.add(idx)
                break

    blocks = []
    total_chars = 0
    for page_no in sorted(chosen):
        text = page_texts[page_no - 1] if page_no - 1 < len(page_texts) else ""
        cleaned = _clean_page_text(text, 3600)
        if not cleaned:
            continue
        block = f"=== Page {page_no} ===\n{cleaned}\n"
        if total_chars + len(block) > max_source_chars:
            break
        blocks.append(block)
        total_chars += len(block)
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _normalize_chat_endpoint(base_url):
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
    if not abs_path or not os.path.isfile(abs_path):
        return None
    with open(abs_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
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
        for idx in range(start, len(raw)):
            ch = raw[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start:idx + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break
        start = raw.find("{", start + 1)
    raise ValueError("模型返回中未找到合法 JSON")


def _normalize_speedread_payload(parsed, candidate_pages):
    candidate_by_page = {int(item.get("page") or 0): item for item in candidate_pages}
    raw_figures = parsed.get("core_figures") if isinstance(parsed, dict) else None
    figures = []
    if isinstance(raw_figures, list):
        for item in raw_figures:
            if not isinstance(item, dict):
                continue
            try:
                page_no = int(item.get("page") or 0)
            except (TypeError, ValueError):
                continue
            if page_no not in candidate_by_page:
                continue
            asset = candidate_by_page[page_no]
            figures.append({
                "page": page_no,
                "title": _speedread_text_value(item.get("title")),
                "what_to_look": _speedread_text_value(item.get("what_to_look")),
                "what_it_proves": _speedread_text_value(item.get("what_it_proves")),
                "why_important": _speedread_text_value(item.get("why_important")),
                "image_path": asset.get("image_path", ""),
                "reason": asset.get("reason", ""),
            })

    if not figures:
        for asset in candidate_pages[:3]:
            figures.append({
                "page": asset.get("page"),
                "title": asset.get("reason") or "关键页面",
                "what_to_look": "优先查看主图、表格标题和图注，再回到正文对应段落交叉验证。",
                "what_it_proves": "如果自动输出缺少更细的证据，请以图注和正文原文为准。",
                "why_important": "该页在自动筛选中最接近方法或实验的核心证据。",
                "image_path": asset.get("image_path", ""),
                "reason": asset.get("reason", ""),
            })

    return {
        "one_sentence": _speedread_text_value(parsed.get("one_sentence") if isinstance(parsed, dict) else None),
        "quick_takeaways": _speedread_list_value(parsed.get("quick_takeaways") if isinstance(parsed, dict) else None, max_items=5),
        "problem_and_motivation": _speedread_text_value(parsed.get("problem_and_motivation") if isinstance(parsed, dict) else None),
        "method_overview": _speedread_text_value(parsed.get("method_overview") if isinstance(parsed, dict) else None),
        "method_steps": _speedread_list_value(parsed.get("method_steps") if isinstance(parsed, dict) else None, max_items=6),
        "core_figures": figures,
        "experiment_read": _speedread_text_value(parsed.get("experiment_read") if isinstance(parsed, dict) else None),
        "contributions": _speedread_list_value(parsed.get("contributions") if isinstance(parsed, dict) else None, max_items=5),
        "limitations": _speedread_list_value(parsed.get("limitations") if isinstance(parsed, dict) else None, max_items=5),
        "deep_read_suggestions": _speedread_list_value(parsed.get("deep_read_suggestions") if isinstance(parsed, dict) else None, max_items=6),
    }


def _request_speedread_from_model(api_config, paper_meta, grounding_text, candidate_pages, workspace_root):
    api_key = (api_config.get("apiKey") or "").strip()
    model = (api_config.get("model") or "").strip()
    if not api_key:
        raise ValueError("未配置 API Key")
    if not model:
        raise ValueError("未配置模型名称")

    endpoint = _normalize_chat_endpoint(api_config.get("apiBaseUrl"))
    try:
        timeout_sec = int(api_config.get("timeoutSec") or 180)
    except (TypeError, ValueError):
        timeout_sec = 180
    timeout_sec = max(30, min(timeout_sec, 600))

    prompt = (
        "你是一个只依据论文原文证据生成内容的论文速读助手。\n"
        "任务目标：帮助用户快速理解论文，不要生成泛泛摘要。\n"
        "写作要求：\n"
        "1. 全部使用中文，信息密度高，但要可读。\n"
        "2. 只依据提供的论文文本片段与候选页图像，不得补充外部知识。\n"
        "3. 如果论文没有明确说明某点，必须写\u201c论文中未明确说明\u201d。\n"
        "4. 不要夸奖论文，不要写营销式语言，不要把摘要直译成中文。\n"
        "5. 重点讲清研究问题、动机、方法如何工作、实验结果说明了什么。\n"
        "6. 图表解读要说明该看哪一页、看什么、它支撑了作者什么论点。\n"
        "7. 仅输出一个合法 JSON 对象，不要输出 Markdown 代码块。\n\n"
        "JSON schema:\n"
        "{\n"
        "  \"one_sentence\": \"一句话总结\",\n"
        "  \"quick_takeaways\": [\"省流点\", \"省流点\"],\n"
        "  \"problem_and_motivation\": \"问题与动机\",\n"
        "  \"method_overview\": \"方法速读\",\n"
        "  \"method_steps\": [\"步骤 1\", \"步骤 2\"],\n"
        "  \"core_figures\": [\n"
        "    {\n"
        "      \"page\": 3,\n"
        "      \"title\": \"图/表标题的中文概括\",\n"
        "      \"what_to_look\": \"读图时要先看什么\",\n"
        "      \"what_it_proves\": \"它支撑了什么结论\",\n"
        "      \"why_important\": \"为什么这张图/表值得优先看\"\n"
        "    }\n"
        "  ],\n"
        "  \"experiment_read\": \"实验速读\",\n"
        "  \"contributions\": [\"贡献 1\", \"贡献 2\"],\n"
        "  \"limitations\": [\"局限 1\", \"局限 2\"],\n"
        "  \"deep_read_suggestions\": [\"建议先精读哪一节、哪几页及原因\"]\n"
        "}\n\n"
        f"论文基础信息：\n标题：{paper_meta.get('title', '')}\n"
        f"作者：{paper_meta.get('authors', '')}\n"
        f"年份：{paper_meta.get('year', '')}\n"
        f"会议/期刊：{paper_meta.get('venue', '')}\n\n"
        "候选关键页（如果在 core_figures 中引用 page，必须从这些候选页里选择）：\n"
        + "\n".join(
            f"- page {item.get('page')}: {item.get('reason')}\n  摘录: {_clean_page_text(item.get('excerpt'), 500)}"
            for item in candidate_pages
        )
        + "\n\n论文文本片段：\n"
        + grounding_text
    )

    content_items = [{"type": "text", "text": prompt}]
    has_image_inputs = False
    for item in candidate_pages:
        image_path = item.get("image_path")
        if not image_path:
            continue
        data_url = _image_to_data_url(image_path, workspace_root)
        if not data_url:
            continue
        has_image_inputs = True
        content_items.append({
            "type": "text",
            "text": f"候选页 page {item.get('page')}，自动标记：{item.get('reason')}。请结合该页图表与前述文本片段交叉解读。",
        })
        content_items.append({
            "type": "image_url",
            "image_url": {"url": data_url},
        })

    # Bypass system proxies to avoid local proxy tools interfering.
    _no_proxy_opener = request.build_opener(request.ProxyHandler({}))

    def send_chat_request(user_content, request_mode):
        payload = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 2200,
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
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            method="POST",
        )

        log(f"send speed-read request: model={model}, mode={request_mode}, endpoint={endpoint}, timeout={timeout_sec}s")
        try:
            with _no_proxy_opener.open(req, timeout=timeout_sec) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                log(f"收到模型回复，长度 {len(body)}")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
            log(f"HTTP 错误 {exc.code}: {detail[:200]}")
            raise RuntimeError(f"模型接口返回 HTTP {exc.code}: {_clean_page_text(detail, 600)}")
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
            log(f"   响应片段: {body[:500]}")
            raise RuntimeError(f"模型接口返回了无效 JSON，可能是错误或网关问题。详情: {_clean_page_text(str(exc), 300)}")

        content = _extract_completion_text(data)
        if not content:
            log(f"无法从响应中提取文本。响应结构: {json.dumps(data, ensure_ascii=False)[:500]}")
            raise RuntimeError("模型接口返回成功，但没有生成文本内容")

        log(f"提取生成文本: {len(content)} 字符")
        return _extract_json_block(content), request_mode

    def should_fallback_to_text_only(message):
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

    if has_image_inputs:
        try:
            return send_chat_request(content_items, "multimodal")
        except RuntimeError as exc:
            if not should_fallback_to_text_only(str(exc)):
                raise
            log("当前接口可能不支持图像输入或多模态消息，已自动降级为纯文本速读重试")

    return send_chat_request(prompt, "text-only")


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def generate_speedread(
    file_key: str,
    api_config: dict,
    force: bool,
    workspace_root: str,
    metadata_file: str,
    speedread_cache_dir: str,
    max_image_pages: int = 4,
    image_width: int = 1400,
    max_source_chars: int = 24000,
) -> _Result:
    """Run the full speed-read pipeline for a paper.

    Returns a _Result(status, payload) suitable for sending as JSON.
    """
    if not file_key:
        return _Result(400, {"ok": False, "error": "缺少 file_key"})
    if not isinstance(api_config, dict):
        return _Result(400, {"ok": False, "error": "apiConfig 格式不正确"})

    try:
        metadata = load_metadata(metadata_file)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"读取 metadata 失败: {exc}"})

    entry, rel_disk, abs_pdf = _resolve_pdf_entry(file_key, metadata, workspace_root)
    if not entry:
        return _Result(404, {"ok": False, "error": f"找不到论文: {file_key}"})
    if not abs_pdf:
        return _Result(404, {"ok": False, "error": f"PDF 不存在: {rel_disk}"})

    existing = entry.get("speed_read") if isinstance(entry.get("speed_read"), dict) else {}
    if existing.get("status") == "success" and not force:
        return _Result(200, {"ok": True, "cached": True, "speed_read": existing})

    now_iso = datetime.now().isoformat(timespec="seconds")
    generating_state = {
        "status": "running",
        "generated_at": existing.get("generated_at") or now_iso,
        "updated_at": now_iso,
        "model": (api_config.get("model") or "").strip(),
        "error": "",
    }
    try:
        current = metadata.get(file_key) or {}
        current["speed_read"] = generating_state
        metadata[file_key] = current
        atomic_write_metadata(metadata, metadata_file)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"写入生成状态失败: {exc}"})

    try:
        page_count = _read_pdf_page_count(abs_pdf)
        page_texts = _extract_pdf_pages_text(abs_pdf, page_count)
        if not page_count:
            page_count = len(page_texts)

        candidate_pages = _select_speedread_pages(page_texts, page_count, max_image_pages)
        enriched_candidates = []
        for item in candidate_pages:
            enriched = dict(item)
            try:
                enriched["image_path"] = _render_speedread_page_image(
                    abs_pdf, file_key, int(item.get("page") or 0),
                    workspace_root, speedread_cache_dir, image_width,
                )
            except Exception as img_exc:
                log(f"速读渲染第 {item.get('page')} 页失败: {img_exc}")
                enriched["image_path"] = ""
            enriched_candidates.append(enriched)

        grounding_text = _build_speedread_grounding_text(page_texts, enriched_candidates, max_source_chars)
        if not grounding_text and not any(item.get("image_path") for item in enriched_candidates):
            raise RuntimeError("无法从 PDF 中提取可用文本或页面图像")

        parsed, request_mode = _request_speedread_from_model(api_config, entry, grounding_text, enriched_candidates, workspace_root)
        normalized = _normalize_speedread_payload(parsed, enriched_candidates)
        success_state = {
            "status": "success",
            "generated_at": existing.get("generated_at") or now_iso,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "model": (api_config.get("model") or "").strip(),
            "api_base_url": _normalize_chat_endpoint(api_config.get("apiBaseUrl")),
            "request_mode": request_mode,
            "page_count": page_count,
            "candidate_pages": [
                {
                    "page": item.get("page"),
                    "reason": item.get("reason", ""),
                    "image_path": item.get("image_path", ""),
                }
                for item in enriched_candidates
            ],
            "content": normalized,
        }

        metadata = load_metadata(metadata_file)
        current = metadata.get(file_key) or {}
        current["speed_read"] = success_state
        metadata[file_key] = current
        atomic_write_metadata(metadata, metadata_file)
        log(f"速读生成完成: {file_key}")
        return _Result(200, {"ok": True, "speed_read": success_state})
    except Exception as exc:
        error_state = {
            "status": "error",
            "generated_at": existing.get("generated_at") or now_iso,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "model": (api_config.get("model") or "").strip(),
            "error": _clean_page_text(str(exc), 600),
        }
        try:
            metadata = load_metadata(metadata_file)
            current = metadata.get(file_key) or {}
            current["speed_read"] = error_state
            metadata[file_key] = current
            atomic_write_metadata(metadata, metadata_file)
        except Exception as write_exc:
            log(f"写入速读错误状态失败: {write_exc}")
        log(f"速读生成失败: {file_key} - {exc}")
        return _Result(500, {"ok": False, "error": _clean_page_text(str(exc), 600), "speed_read": error_state})


def test_speedread_config(api_config: dict, workspace_root: str) -> _Result:
    """Test AI API connectivity with a minimal request."""
    if not isinstance(api_config, dict):
        return _Result(400, {"ok": False, "error": "apiConfig 格式不正确"})

    test_paper_meta = {
        "title": "接口测试论文",
        "authors": "OpenPaper",
        "year": "2026",
        "venue": "Config Test",
    }
    test_grounding_text = (
        "This is a short connectivity test prompt for API and model availability. "
        "Return valid JSON only, and do not use external knowledge. "
        "Assume the paper proposes a method and reports effective experiment results."
    )
    try:
        parsed, request_mode = _request_speedread_from_model(api_config, test_paper_meta, test_grounding_text, [], workspace_root)
        normalized = _normalize_speedread_payload(parsed, [])
        return _Result(200, {
            "ok": True,
            "request_mode": request_mode,
            "preview": normalized.get("one_sentence") or "测试成功",
        })
    except Exception as exc:
        return _Result(500, {"ok": False, "error": _clean_page_text(str(exc), 600)})
