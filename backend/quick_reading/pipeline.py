from __future__ import annotations

import hashlib

from backend.utils import log

from .figure_extract import FIGURE_EXTRACTOR_VERSION, FigureExtractionService
from .llm_client import OpenAICompatibleClient
from .models import (
    _Result,
    clean_page_text,
    normalize_generation_options,
    normalize_speedread_payload,
    now_iso,
    paper_snapshot,
)
from .pdf_extract import (
    build_grounding_text,
    extract_pdf_pages_text,
    load_metadata_for_generation,
    read_pdf_page_count,
    resolve_pdf_entry,
)
from .prompts import build_speedread_prompt
from .storage import load_speedread_cache, write_speedread_cache

SPEEDREAD_CACHE_VERSION = 3


def _pdf_fingerprint(abs_pdf):
    digest = hashlib.sha256()
    size = 0
    with open(abs_pdf, "rb") as file_obj:
        while True:
            chunk = file_obj.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def _output_token_budget(depth):
    if depth == "brief":
        return 3600
    if depth == "deep":
        return 7200
    return 5200


def _token_status(grounding_text, max_source_chars, max_tokens):
    source_chars = len(grounding_text or "")
    return {
        "source_chars": source_chars,
        "source_chars_limit": max_source_chars,
        "estimated_input_tokens": max(1, source_chars // 4) if source_chars else 0,
        "max_output_tokens": max_tokens,
    }


def generate_speedread(
    file_key: str,
    api_config: dict,
    force: bool,
    workspace_root: str,
    metadata_file: str,
    quick_reading_cache_dir: str,
    max_image_pages: int = 5,
    image_width: int = 3200,
    max_source_chars: int = 24000,
    options=None,
) -> _Result:
    if not file_key:
        return _Result(400, {"ok": False, "error": "缺少 file_key"})
    if not isinstance(api_config, dict):
        return _Result(400, {"ok": False, "error": "apiConfig 格式不正确"})

    generation_options = normalize_generation_options(options, max_image_pages)

    try:
        metadata = load_metadata_for_generation(metadata_file)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"读取 metadata 失败: {exc}"})

    entry, rel_disk, abs_pdf = resolve_pdf_entry(file_key, metadata, workspace_root)
    if not entry:
        return _Result(404, {"ok": False, "error": f"找不到论文: {file_key}"})
    if not abs_pdf:
        return _Result(404, {"ok": False, "error": f"PDF 不存在: {rel_disk}"})

    try:
        pdf_fingerprint = _pdf_fingerprint(abs_pdf)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"读取 PDF 指纹失败: {exc}"})

    try:
        existing = load_speedread_cache(file_key, workspace_root, quick_reading_cache_dir)
    except Exception:
        existing = {}
    cache_is_current = (
        existing.get("version") == SPEEDREAD_CACHE_VERSION
        and existing.get("extractor_version") == FIGURE_EXTRACTOR_VERSION
        and existing.get("pdf_fingerprint") == pdf_fingerprint
    )
    if existing.get("status") == "success" and cache_is_current and not force:
        return _Result(200, {"ok": True, "cached": True, "speed_read": existing})

    generated_at = existing.get("generated_at") or now_iso()
    paper_meta = paper_snapshot(file_key, entry)
    model_name = (api_config.get("model") or "").strip()

    def write_progress(stage, message, percent, extra=None):
        progress = {
            "stage": stage,
            "message": message,
            "percent": percent,
            "updated_at": now_iso(),
        }
        if isinstance(extra, dict):
            progress.update(extra)
        running_state = {
            "version": SPEEDREAD_CACHE_VERSION,
            "extractor_version": FIGURE_EXTRACTOR_VERSION,
            "pdf_fingerprint": pdf_fingerprint,
            "status": "running",
            "generated_at": generated_at,
            "updated_at": now_iso(),
            "model": model_name,
            "request_mode": "",
            "paper": paper_meta,
            "options": generation_options,
            "progress": progress,
        }
        write_speedread_cache(file_key, running_state, workspace_root, quick_reading_cache_dir)

    try:
        write_progress("queued", "已创建速读任务", 5)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"写入生成状态失败: {exc}"})

    try:
        write_progress("pdf", "正在读取 PDF 文本和页数", 14)
        page_count = read_pdf_page_count(abs_pdf)
        page_texts = extract_pdf_pages_text(abs_pdf, page_count)
        if not page_count:
            page_count = len(page_texts)

        max_figures = generation_options["max_figures"] if generation_options.get("include_figure_insights") else 0
        write_progress("figures", "正在筛选并渲染关键图表页", 30, {"page_count": page_count})
        figure_service = FigureExtractionService(workspace_root, quick_reading_cache_dir, image_width)
        figure_assets = figure_service.extract_important_figures(file_key, abs_pdf, page_texts, max_figures)

        write_progress("grounding", "正在构造论文证据片段", 46, {"candidate_count": len(figure_assets)})
        grounding_text = build_grounding_text(page_texts, figure_assets, max_source_chars)
        if not grounding_text and not any(item.get("image_path") for item in figure_assets):
            raise RuntimeError("无法从 PDF 中提取可用文本或页面图像")

        max_tokens = _output_token_budget(generation_options.get("depth"))
        token_status = _token_status(grounding_text, max_source_chars, max_tokens)
        write_progress("model", "正在调用模型生成结构化速读", 62, {"token_status": token_status})
        prompt = build_speedread_prompt(entry, grounding_text, figure_assets, generation_options)
        client = OpenAICompatibleClient(api_config, workspace_root)
        parsed, request_mode, endpoint = client.request_json(prompt, figure_assets, max_tokens=max_tokens)

        write_progress("normalize", "正在整理模型输出和图表解释", 88, {"token_status": token_status})
        normalized = normalize_speedread_payload(parsed, figure_assets, generation_options.get("depth"))
        success_state = {
            "version": SPEEDREAD_CACHE_VERSION,
            "extractor_version": FIGURE_EXTRACTOR_VERSION,
            "pdf_fingerprint": pdf_fingerprint,
            "status": "success",
            "generated_at": generated_at,
            "updated_at": now_iso(),
            "model": model_name,
            "api_base_url": endpoint,
            "request_mode": request_mode,
            "paper": paper_meta,
            "options": generation_options,
            "page_count": page_count,
            "candidate_pages": [
                {
                    "id": item.get("id", ""),
                    "kind": item.get("kind", "page_snapshot"),
                    "label": item.get("label", ""),
                    "caption": item.get("caption", ""),
                    "page": item.get("page"),
                    "image_path": item.get("image_path", ""),
                    "confidence": item.get("confidence", 0),
                    "crop_rect": item.get("crop_rect") or {},
                    "reason": item.get("reason", ""),
                }
                for item in figure_assets
            ],
            "content": normalized,
        }

        write_speedread_cache(file_key, success_state, workspace_root, quick_reading_cache_dir)
        log(f"速读生成完成: {file_key}")
        return _Result(200, {"ok": True, "speed_read": success_state})
    except Exception as exc:
        error_state = {
            "version": SPEEDREAD_CACHE_VERSION,
            "extractor_version": FIGURE_EXTRACTOR_VERSION,
            "pdf_fingerprint": pdf_fingerprint,
            "status": "error",
            "generated_at": generated_at,
            "updated_at": now_iso(),
            "model": model_name,
            "request_mode": "",
            "paper": paper_meta,
            "options": generation_options,
            "error": clean_page_text(str(exc), 600),
            "progress": {
                "stage": "error",
                "message": "速读生成失败",
                "percent": 100,
                "updated_at": now_iso(),
            },
        }
        try:
            write_speedread_cache(file_key, error_state, workspace_root, quick_reading_cache_dir)
        except Exception as write_exc:
            log(f"写入速读错误状态失败: {write_exc}")
        log(f"速读生成失败: {file_key} - {exc}")
        return _Result(500, {"ok": False, "error": clean_page_text(str(exc), 600), "speed_read": error_state})


def test_speedread_config(api_config: dict, workspace_root: str) -> _Result:
    if not isinstance(api_config, dict):
        return _Result(400, {"ok": False, "error": "apiConfig 格式不正确"})

    test_paper_meta = {
        "title": "接口测试论文",
        "authors": "OpenPaper",
        "year": "2026",
        "venue": "Config Test",
    }
    test_grounding_text = (
        "=== Page 1 ===\n"
        "This is a short connectivity test prompt for API and model availability. "
        "Return valid JSON only. The paper compares a proposed method with a baseline, "
        "uses one dataset, and reports one metric."
    )
    try:
        options = normalize_generation_options({"include_figure_insights": False}, 3)
        prompt = build_speedread_prompt(test_paper_meta, test_grounding_text, [], options)
        client = OpenAICompatibleClient(api_config, workspace_root)
        parsed, request_mode, _endpoint = client.request_json(prompt, [], max_tokens=3000)
        normalized = normalize_speedread_payload(parsed, [])
        return _Result(200, {
            "ok": True,
            "request_mode": request_mode,
            "preview": normalized.get("one_sentence") or "测试成功",
        })
    except Exception as exc:
        return _Result(500, {"ok": False, "error": clean_page_text(str(exc), 600)})
