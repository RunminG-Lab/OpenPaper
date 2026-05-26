from __future__ import annotations

import re
from collections import namedtuple
from datetime import datetime

_Result = namedtuple("_Result", ["status", "payload"])

MISSING_TEXT = "论文中未明确说明"
ALLOWED_DEPTHS = {"brief", "standard", "deep"}
ALLOWED_METHOD_ROLES = {"proposed", "baseline", "ablation", "variant", "other"}
ALLOWED_DIFFICULTIES = {"low", "medium", "high", "unknown"}
ALLOWED_TERM_BASES = {"paper", "general_background"}
DEFAULT_FOCUS = ["general", "experiment", "reproduction", "figure"]
MAX_FIGURE_CANDIDATES = 12
LEGACY_FIGURE_LABEL_RE = re.compile(
    r"^\s*(展示什么|看什么|先看哪里|视觉元素|支撑结论|它说明了什么|为什么重要|方法/实验关系|容易误读)\s*[:：]\s*"
)
FIGURE_REF_RE = re.compile(r"\b(Figure|Fig\.?|Table|图|表)\s*([\dIVXLC]+[A-Za-z0-9.\-]*)", re.I)
MISSING_FIGURE_SUMMARY = "模型未返回该图表的中文解读，请以截图和原文图注为准。"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def clean_page_text(text, limit=None):
    cleaned = (text or "").replace("\x00", "")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    if limit and len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + " ..."
    return cleaned


def text_value(value, fallback=MISSING_TEXT, limit=None):
    if not isinstance(value, str):
        return fallback
    cleaned = clean_page_text(value, limit)
    return cleaned or fallback


def clean_figure_summary(value, fallback=MISSING_TEXT, limit=None):
    text = text_value(value, "", limit)
    if not text:
        return fallback
    kept = []
    for line in re.split(r"\n+", text):
        cleaned = LEGACY_FIGURE_LABEL_RE.sub("", line).strip()
        if not cleaned or cleaned == MISSING_TEXT:
            continue
        kept.append(cleaned)
    return clean_page_text(" ".join(kept), limit) or fallback


def list_value(value, fallback_text=MISSING_TEXT, max_items=8):
    if not isinstance(value, list):
        return [fallback_text]
    items = []
    for item in value:
        if isinstance(item, str):
            text = text_value(item, "")
        elif isinstance(item, dict):
            text = text_value(item.get("text") or item.get("summary") or item.get("name"), "")
        else:
            text = ""
        if text:
            items.append(text)
        if len(items) >= max_items:
            break
    return items or [fallback_text]


def normalize_page_refs(value):
    if not isinstance(value, list):
        return []
    refs = []
    for item in value:
        try:
            page_no = int(item)
        except (TypeError, ValueError):
            continue
        if page_no > 0 and page_no not in refs:
            refs.append(page_no)
    return refs


def normalize_generation_options(options, default_max_figures=5):
    source = options if isinstance(options, dict) else {}
    depth = (source.get("depth") or "standard").strip().lower() if isinstance(source.get("depth"), str) else "standard"
    if depth not in ALLOWED_DEPTHS:
        depth = "standard"

    focus_source = source.get("focus")
    if isinstance(focus_source, list):
        focus = []
        for item in focus_source:
            if not isinstance(item, str):
                continue
            normalized = item.strip().lower()
            if normalized and normalized not in focus:
                focus.append(normalized)
    else:
        focus = []
    if not focus:
        focus = list(DEFAULT_FOCUS)

    extra_prompt = text_value(source.get("extra_prompt"), "", 2000)
    include_figure_insights = source.get("include_figure_insights")
    include_figure_insights = True if include_figure_insights is None else bool(include_figure_insights)

    try:
        max_figures = int(source.get("max_figures") or default_max_figures or 5)
    except (TypeError, ValueError):
        max_figures = int(default_max_figures or 5)
    max_figures = max(3, min(MAX_FIGURE_CANDIDATES, max_figures))

    return {
        "depth": depth,
        "focus": focus,
        "extra_prompt": extra_prompt,
        "include_figure_insights": include_figure_insights,
        "max_figures": max_figures,
    }


def paper_snapshot(file_key, entry):
    entry = entry if isinstance(entry, dict) else {}
    return {
        "file_key": file_key or "",
        "title": entry.get("title", ""),
        "authors": entry.get("authors", ""),
        "venue": entry.get("venue", ""),
        "year": entry.get("year", ""),
    }


def _dict_text(record, key, fallback=MISSING_TEXT):
    if not isinstance(record, dict):
        return fallback
    return text_value(record.get(key), fallback)


def _normalize_compared_methods(value):
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        if isinstance(item, str):
            records.append({
                "name": text_value(item),
                "role": "other",
                "description": MISSING_TEXT,
                "result": MISSING_TEXT,
                "page_refs": [],
            })
            continue
        if not isinstance(item, dict):
            continue
        role = text_value(item.get("role"), "other").lower()
        if role not in ALLOWED_METHOD_ROLES:
            role = "other"
        records.append({
            "name": _dict_text(item, "name"),
            "role": role,
            "description": _dict_text(item, "description"),
            "result": _dict_text(item, "result"),
            "page_refs": normalize_page_refs(item.get("page_refs")),
        })
    return records


def _normalize_datasets(value):
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        if isinstance(item, str):
            records.append({
                "name": text_value(item),
                "task": MISSING_TEXT,
                "scale": MISSING_TEXT,
                "split": MISSING_TEXT,
                "notes": MISSING_TEXT,
                "page_refs": [],
            })
            continue
        if not isinstance(item, dict):
            continue
        records.append({
            "name": _dict_text(item, "name"),
            "task": _dict_text(item, "task"),
            "scale": _dict_text(item, "scale"),
            "split": _dict_text(item, "split"),
            "notes": _dict_text(item, "notes"),
            "page_refs": normalize_page_refs(item.get("page_refs")),
        })
    return records


def _normalize_metrics(value):
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        if isinstance(item, str):
            records.append({
                "name": text_value(item),
                "meaning": MISSING_TEXT,
                "why_used": MISSING_TEXT,
                "limitation": MISSING_TEXT,
                "page_refs": [],
            })
            continue
        if not isinstance(item, dict):
            continue
        records.append({
            "name": _dict_text(item, "name"),
            "meaning": _dict_text(item, "meaning"),
            "why_used": _dict_text(item, "why_used"),
            "limitation": _dict_text(item, "limitation"),
            "page_refs": normalize_page_refs(item.get("page_refs")),
        })
    return records


def _normalize_main_results(value):
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        if isinstance(item, str):
            records.append({
                "claim": text_value(item),
                "evidence": MISSING_TEXT,
                "page_refs": [],
            })
            continue
        if not isinstance(item, dict):
            continue
        records.append({
            "claim": text_value(item.get("claim") or item.get("result") or item.get("summary")),
            "evidence": text_value(item.get("evidence") or item.get("detail")),
            "page_refs": normalize_page_refs(item.get("page_refs")),
        })
    return records


def _normalize_technical_modules(value):
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        if isinstance(item, str):
            records.append({
                "name": text_value(item),
                "role": MISSING_TEXT,
                "mechanism": MISSING_TEXT,
                "technical_explanation": MISSING_TEXT,
                "terms": [MISSING_TEXT],
                "page_refs": [],
            })
            if len(records) >= 8:
                break
            continue
        if not isinstance(item, dict):
            continue
        records.append({
            "name": _dict_text(item, "name"),
            "role": _dict_text(item, "role"),
            "mechanism": _dict_text(item, "mechanism"),
            "technical_explanation": _dict_text(item, "technical_explanation"),
            "terms": list_value(item.get("terms"), max_items=6),
            "page_refs": normalize_page_refs(item.get("page_refs")),
        })
        if len(records) >= 8:
            break
    return records


def _normalize_technical_terms(value):
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        if isinstance(item, str):
            records.append({
                "term": text_value(item),
                "explanation": MISSING_TEXT,
                "paper_context": MISSING_TEXT,
                "basis": "paper",
            })
            if len(records) >= 12:
                break
            continue
        if not isinstance(item, dict):
            continue
        basis = text_value(item.get("basis"), "paper").lower()
        if basis not in ALLOWED_TERM_BASES:
            basis = "paper"
        records.append({
            "term": _dict_text(item, "term"),
            "explanation": _dict_text(item, "explanation"),
            "paper_context": _dict_text(item, "paper_context"),
            "basis": basis,
        })
        if len(records) >= 12:
            break
    return records


def normalize_technical_detail_read(value):
    if isinstance(value, str):
        return {
            "summary": text_value(value),
            "modules": [],
            "terms": [],
            "background_notes": [MISSING_TEXT],
            "missing_details": [MISSING_TEXT],
        }
    source = value if isinstance(value, dict) else {}
    return {
        "summary": text_value(source.get("summary")),
        "modules": _normalize_technical_modules(source.get("modules")),
        "terms": _normalize_technical_terms(source.get("terms")),
        "background_notes": list_value(source.get("background_notes"), max_items=8),
        "missing_details": list_value(source.get("missing_details"), max_items=8),
    }


def normalize_experiment_read(value):
    if isinstance(value, str):
        return {
            "summary": text_value(value),
            "compared_methods": [],
            "datasets": [],
            "metrics": [],
            "main_results": [],
            "reproduction_difficulty": "unknown",
            "reproduction_notes": [MISSING_TEXT],
            "workload_estimate": MISSING_TEXT,
            "evaluation_design_notes": [MISSING_TEXT],
            "missing_details": ["旧格式缓存未提供结构化实验字段"],
        }
    source = value if isinstance(value, dict) else {}
    difficulty = text_value(source.get("reproduction_difficulty"), "unknown").lower()
    if difficulty not in ALLOWED_DIFFICULTIES:
        difficulty = "unknown"
    return {
        "summary": text_value(source.get("summary")),
        "compared_methods": _normalize_compared_methods(source.get("compared_methods")),
        "datasets": _normalize_datasets(source.get("datasets")),
        "metrics": _normalize_metrics(source.get("metrics")),
        "main_results": _normalize_main_results(source.get("main_results")),
        "reproduction_difficulty": difficulty,
        "reproduction_notes": list_value(source.get("reproduction_notes"), max_items=8),
        "workload_estimate": text_value(source.get("workload_estimate")),
        "evaluation_design_notes": list_value(source.get("evaluation_design_notes"), max_items=8),
        "missing_details": list_value(source.get("missing_details"), max_items=8),
    }


def _asset_maps(figure_assets):
    by_id = {}
    by_page = {}
    by_label = {}
    by_ref = {}
    for asset in figure_assets or []:
        if not isinstance(asset, dict):
            continue
        asset_id = asset.get("id")
        if asset_id:
            by_id[str(asset_id)] = asset
        label = _clean_match_token(asset.get("label") or asset.get("caption"))
        if label:
            by_label[label] = asset
        ref_key = _asset_ref_key(asset)
        if ref_key:
            by_ref.setdefault(ref_key, []).append(asset)
        try:
            page_no = int(asset.get("page") or 0)
        except (TypeError, ValueError):
            page_no = 0
        if page_no:
            by_page.setdefault(page_no, []).append(asset)
    return by_id, by_page, by_label, by_ref


def _clean_match_token(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _figure_ref_key(value):
    match = FIGURE_REF_RE.search(str(value or ""))
    if not match:
        return ""
    raw_kind = match.group(1).lower()
    kind = "table" if raw_kind == "table" or raw_kind.startswith("表") else "figure"
    number = _clean_match_token(match.group(2))
    return f"{kind}:{number}" if number else ""


def _asset_ref_key(asset):
    if not isinstance(asset, dict):
        return ""
    return _figure_ref_key(asset.get("label")) or _figure_ref_key(asset.get("caption"))


def _asset_page(asset):
    try:
        return int(asset.get("page") or 0)
    except (TypeError, ValueError):
        return 0


def _item_page(item):
    try:
        return int(item.get("page") or 0)
    except (TypeError, ValueError):
        return 0


def _item_ref_key(item):
    if not isinstance(item, dict):
        return ""
    return _figure_ref_key(item.get("label")) or _figure_ref_key(item.get("caption")) or _figure_ref_key(item.get("title"))


def _asset_used(asset, used_asset_ids):
    asset_id = str(asset.get("id") or "") if isinstance(asset, dict) else ""
    return bool(asset_id and asset_id in used_asset_ids)


def _item_asset_conflicts(item, asset):
    item_page = _item_page(item)
    asset_page = _asset_page(asset)
    if item_page and asset_page and item_page != asset_page:
        return True
    item_ref = _item_ref_key(item)
    asset_ref = _asset_ref_key(asset)
    return bool(item_ref and asset_ref and item_ref != asset_ref)


def _caption_like_english_summary(value, asset=None):
    text = clean_page_text(value)
    if not text:
        return False
    if re.search(r"[\u4e00-\u9fff]", text):
        return False
    if re.match(r"\s*(Figure|Fig\.?|Table)\s+[\dIVXLC]+", text, re.I):
        return True
    caption = clean_page_text(asset.get("caption") if isinstance(asset, dict) else "")
    return bool(caption and _clean_match_token(text) == _clean_match_token(caption))


def _figure_summary_limit(depth):
    if depth == "brief":
        return 140
    if depth == "deep":
        return 300
    return 220


def _compact_figure_summary(*values):
    parts = []
    for value in values:
        text = clean_figure_summary(value, "", 420)
        if text and text != MISSING_TEXT and text not in parts:
            parts.append(text)
    return " ".join(parts) if parts else MISSING_TEXT


def _summary_from_asset(asset, limit=220):
    return MISSING_FIGURE_SUMMARY


def _summary_from_item(item, asset, limit=220):
    direct = clean_figure_summary(
        item.get("summary") or item.get("interpretation") or item.get("analysis") or item.get("description"),
        "",
        limit,
    )
    if direct and not _caption_like_english_summary(direct, asset):
        return direct
    summary = _compact_figure_summary(
        item.get("what_it_shows") or item.get("what_to_look"),
        item.get("how_to_read"),
        item.get("supports_claim") or item.get("what_it_proves"),
        item.get("relation_to_method_or_experiment") or item.get("why_important"),
        item.get("misread_risks"),
    )
    if summary != MISSING_TEXT and not _caption_like_english_summary(summary, asset):
        return summary
    return _summary_from_asset(asset, limit) if isinstance(asset, dict) else MISSING_TEXT


def _figure_from_asset(asset, summary_limit=220):
    summary = _summary_from_asset(asset, summary_limit)
    return {
        "id": asset.get("id", ""),
        "kind": asset.get("kind", "page_snapshot"),
        "label": asset.get("label", "关键页面"),
        "caption": asset.get("caption", MISSING_TEXT),
        "page": asset.get("page"),
        "image_path": asset.get("image_path", ""),
        "confidence": asset.get("confidence", 0),
        "title": asset.get("label") or asset.get("reason") or "关键图表页",
        "summary": summary,
        "page_refs": normalize_page_refs([asset.get("page")]),
    }


def _match_figure_asset(item, by_id, by_page, by_label, by_ref, used_asset_ids):
    figure_id = str(item.get("figure_id") or item.get("id") or item.get("candidate_id") or "")
    if figure_id and figure_id in by_id:
        asset = by_id[figure_id]
        if not _asset_used(asset, used_asset_ids) and not _item_asset_conflicts(item, asset):
            return asset

    item_ref = _item_ref_key(item)
    page_no = _item_page(item)
    if item_ref:
        candidates = [asset for asset in by_ref.get(item_ref, []) if not _asset_used(asset, used_asset_ids)]
        if page_no:
            candidates = [asset for asset in candidates if _asset_page(asset) == page_no]
        if len(candidates) == 1:
            return candidates[0]

    label_token = _clean_match_token(item.get("label") or item.get("caption") or item.get("title") or figure_id)
    if label_token and label_token in by_label:
        asset = by_label[label_token]
        if not _asset_used(asset, used_asset_ids) and (not page_no or _asset_page(asset) == page_no):
            return asset
    if figure_id:
        return {}

    page_assets = [asset for asset in by_page.get(page_no, []) if str(asset.get("id") or "") not in used_asset_ids]
    if len(page_assets) == 1:
        return page_assets[0]
    return {}


def normalize_figure_insights(value, figure_assets, depth="standard"):
    by_id, by_page, by_label, by_ref = _asset_maps(figure_assets)
    insights = []
    used_asset_ids = set()
    summary_limit = _figure_summary_limit(depth)
    has_model_items = isinstance(value, list)
    if has_model_items:
        for item in value:
            if not isinstance(item, dict):
                continue
            figure_id = item.get("figure_id") or item.get("id") or item.get("candidate_id") or ""
            asset = _match_figure_asset(item, by_id, by_page, by_label, by_ref, used_asset_ids)
            if not asset:
                continue
            page_no = _asset_page(asset) or _item_page(item)
            asset_id = str(asset.get("id") or figure_id)
            if asset_id:
                used_asset_ids.add(asset_id)
            summary = _summary_from_item(item, asset, summary_limit)
            insights.append({
                "id": asset_id or str(figure_id),
                "kind": asset.get("kind") or item.get("kind") or "page_snapshot",
                "label": text_value(asset.get("label") or item.get("label"), asset.get("label") or "关键页面"),
                "caption": text_value(asset.get("caption") or item.get("caption"), asset.get("caption") or MISSING_TEXT),
                "page": page_no or asset.get("page"),
                "image_path": asset.get("image_path") or item.get("image_path") or "",
                "confidence": asset.get("confidence", item.get("confidence", 0)),
                "title": text_value(item.get("title") or item.get("what_it_shows") or asset.get("reason"), "关键图表页"),
                "summary": summary if summary != MISSING_TEXT else MISSING_FIGURE_SUMMARY,
                "page_refs": normalize_page_refs([page_no or asset.get("page")]),
            })
    if has_model_items:
        return insights
    for asset in figure_assets or []:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("id") or "")
        if asset_id and asset_id in used_asset_ids:
            continue
        insights.append(_figure_from_asset(asset, summary_limit))
        if asset_id:
            used_asset_ids.add(asset_id)
    if insights:
        max_items = len(figure_assets or []) or len(insights)
        return insights[:max_items]
    return [_figure_from_asset(asset, summary_limit) for asset in figure_assets or [] if isinstance(asset, dict)]


def normalize_speedread_payload(parsed, figure_assets, depth="standard"):
    source = parsed if isinstance(parsed, dict) else {}
    figure_source = source.get("figure_insights")
    if not isinstance(figure_source, list) and isinstance(source.get("core_figures"), list):
        figure_source = source.get("core_figures")
    return {
        "one_sentence": text_value(source.get("one_sentence")),
        "quick_takeaways": list_value(source.get("quick_takeaways"), max_items=6),
        "problem_and_motivation": text_value(source.get("problem_and_motivation")),
        "method_overview": text_value(source.get("method_overview")),
        "method_steps": list_value(source.get("method_steps"), max_items=8),
        "technical_detail_read": normalize_technical_detail_read(source.get("technical_detail_read")),
        "experiment_read": normalize_experiment_read(source.get("experiment_read")),
        "contributions": list_value(source.get("contributions"), max_items=6),
        "limitations": list_value(source.get("limitations"), max_items=6),
        "figure_insights": normalize_figure_insights(figure_source, figure_assets, depth),
        "deep_read_suggestions": list_value(source.get("deep_read_suggestions"), max_items=8),
    }
