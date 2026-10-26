from __future__ import annotations

import hashlib

from backend.utils import log

from .figure_extract import FIGURE_EXTRACTOR_VERSION, FigureExtractionService
from .llm_client import OpenAICompatibleClient
from .model_catalog import get_model_capabilities
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
from .token_budget import plan_token_budget, positive_int

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


def _read_model_capabilities(api_config):
    try:
        return get_model_capabilities(api_config)
    except RuntimeError:
        # Older compatible endpoints need not implement discovery to remain usable.
        return {}


def _prepare_model_request(entry, grounding_text, figure_assets, options, api_config, capabilities, max_source_chars):
    source_text = (grounding_text or "")[:max_source_chars]
    original_source_length = len(source_text)
    candidates = list(figure_assets or [])
    text_only = capabilities.get("supports_vision") is False
    if text_only and not source_text and not any(item.get("caption") or item.get("excerpt") for item in candidates):
        raise RuntimeError("当前模型不支持图片，且 PDF 没有可用文本或图表字幕；请改用支持图片输入的模型。")
    minimum_evidence_length = min(original_source_length, 1000)
    manual_output = positive_int(api_config.get("maxOutputTokens"))

    def evaluate(evidence, figures):
        prompt = build_speedread_prompt(entry, evidence, figures, options)
        if text_only and figures:
            prompt += "\n\n当前模型不支持图片输入，本次仅提供图表字幕和文字摘录；不得声称已检查截图中的视觉细节。"
        image_count = 0 if text_only else sum(bool(item.get("image_path")) for item in figures)
        attachment_text = "" if text_only else "\n".join(
            f"{item.get('id')} / {item.get('page')} / {item.get('reason')}"
            for item in figures if item.get("image_path")
        )
        budget = plan_token_budget(prompt + attachment_text, image_count, options.get("depth"), api_config, capabilities, figure_count=len(figures))
        return prompt, budget

    def fits(budget):
        # A manual value is an output ceiling, not a reason to discard usable evidence.
        if manual_output:
            return budget["max_output_tokens"] >= min(2048, budget["target_output_tokens"])
        return budget["target_fits"]

    prompt, budget = evaluate(source_text, candidates)
    if not fits(budget):
        # Rebuild the whole prompt while trimming evidence before removing images.
        while candidates:
            _empty_prompt, empty_budget = evaluate(source_text[:minimum_evidence_length], candidates)
            if fits(empty_budget):
                break
            candidates.pop()
        _empty_prompt, empty_budget = evaluate(source_text[:minimum_evidence_length], candidates)
        if fits(empty_budget):
            low, high = 0, len(source_text)
            while low < high:
                midpoint = (low + high + 1) // 2
                _candidate_prompt, candidate_budget = evaluate(source_text[:midpoint], candidates)
                if fits(candidate_budget):
                    low = midpoint
                else:
                    high = midpoint - 1
            source_text = source_text[:low]
        else:
            source_text = ""
        prompt, budget = evaluate(source_text, candidates)

    if budget["max_output_tokens"] < min(2048, budget["target_output_tokens"]):
        raise RuntimeError("模型上下文不足以容纳速读提示词和最小输出预算；请使用更大上下文模型，或调低手动输出上限。")
    if not source_text and original_source_length:
        raise RuntimeError("模型上下文不足以保留论文文本证据；请使用更大上下文模型，或调低手动输出上限。")

    budget["source_chars"] = len(source_text)
    budget["source_chars_limit"] = max_source_chars
    budget["source_trimmed"] = len(source_text) < original_source_length
    budget["omitted_figures"] = len(figure_assets or []) - len(candidates)
    budget["supports_vision"] = capabilities.get("supports_vision")
    if budget["source_trimmed"]:
        budget["budget_notes"].append("已按上下文上限缩短论文证据，随后重新计算完整提示词。")
    if budget["omitted_figures"]:
        budget["budget_notes"].append(f"上下文不足，已减少 {budget['omitted_figures']} 张候选图表。")
    if text_only:
        budget["budget_notes"].append("官方标记该模型不支持图片，本次使用纯文本及图表字幕。")
    return prompt, candidates, budget


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

    token_status = {}
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

        write_progress("grounding", "正在读取模型上下文、输出和图片能力", 54, {"candidate_count": len(figure_assets)})
        capabilities = _read_model_capabilities(api_config)
        prompt, figure_assets, token_status = _prepare_model_request(
            entry, grounding_text, figure_assets, generation_options, api_config, capabilities, max_source_chars,
        )
        mode_note = "（模型不支持图片，使用文本和图表字幕）" if capabilities.get("supports_vision") is False else ""
        write_progress("model", f"正在调用模型生成结构化速读{mode_note}", 62, {"token_status": token_status})
        client = OpenAICompatibleClient(api_config, workspace_root)
        request_figures = [] if capabilities.get("supports_vision") is False else figure_assets
        parsed, request_mode, endpoint = client.request_json(prompt, request_figures, max_tokens=token_status["max_output_tokens"])

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
            "token_status": token_status,
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
            "token_status": token_status,
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

    try:
        prompt = '这是接口连通性测试，无需分析论文。只返回一个 JSON 对象：{"ok":true}。'
        capabilities = _read_model_capabilities(api_config)
        test_config = dict(api_config)
        # Connectivity checks do not need a complete paper review or its budget.
        configured_output = positive_int(api_config.get("maxOutputTokens"))
        test_config["maxOutputTokens"] = min(1024, configured_output or 1024)
        budget = plan_token_budget(prompt, 0, "brief", test_config, capabilities)
        if budget["max_output_tokens"] < 1:
            raise RuntimeError("模型上下文上限不足以执行接口测试")
        client = OpenAICompatibleClient(api_config, workspace_root)
        parsed, request_mode, _endpoint = client.request_json(prompt, [], max_tokens=budget["max_output_tokens"])
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            raise RuntimeError('接口已连通，但模型未按测试要求返回 {"ok":true}')
        return _Result(200, {
            "ok": True,
            "request_mode": request_mode,
            "preview": "接口连通，已收到 JSON 回答",
            "token_status": budget,
            "model_capabilities": capabilities,
        })
    except Exception as exc:
        return _Result(500, {"ok": False, "error": clean_page_text(str(exc), 600)})
