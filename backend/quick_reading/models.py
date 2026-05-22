from __future__ import annotations

import re
from collections import namedtuple
from datetime import datetime

_Result = namedtuple("_Result", ["status", "payload"])

MISSING_TEXT = "论文中未明确说明"
ALLOWED_DEPTHS = {"brief", "standard", "deep"}
ALLOWED_METHOD_ROLES = {"proposed", "baseline", "ablation", "variant", "other"}
ALLOWED_DIFFICULTIES = {"low", "medium", "high", "unknown"}
DEFAULT_FOCUS = ["general", "experiment", "reproduction", "figure"]


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
    max_figures = max(3, min(8, max_figures))

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
    for asset in figure_assets or []:
        if not isinstance(asset, dict):
            continue
        asset_id = asset.get("id")
        if asset_id:
            by_id[str(asset_id)] = asset
        try:
            page_no = int(asset.get("page") or 0)
        except (TypeError, ValueError):
            page_no = 0
        if page_no and page_no not in by_page:
            by_page[page_no] = asset
    return by_id, by_page


def _figure_from_asset(asset):
    return {
        "id": asset.get("id", ""),
        "kind": asset.get("kind", "page_snapshot"),
        "label": asset.get("label", "关键页面"),
        "caption": asset.get("caption", MISSING_TEXT),
        "page": asset.get("page"),
        "image_path": asset.get("image_path", ""),
        "confidence": asset.get("confidence", 0),
        "title": asset.get("label") or asset.get("reason") or "关键图表页",
        "what_it_shows": asset.get("caption") or asset.get("excerpt") or MISSING_TEXT,
        "how_to_read": "优先查看图题/表题、图注和正文中引用该图表的段落，再看坐标轴、模块箭头或表格列。",
        "visual_elements": MISSING_TEXT,
        "supports_claim": MISSING_TEXT,
        "relation_to_method_or_experiment": MISSING_TEXT,
        "misread_risks": MISSING_TEXT,
        "page_refs": normalize_page_refs([asset.get("page")]),
    }


def normalize_figure_insights(value, figure_assets):
    by_id, by_page = _asset_maps(figure_assets)
    insights = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            figure_id = item.get("figure_id") or item.get("id") or item.get("candidate_id") or ""
            try:
                page_no = int(item.get("page") or 0)
            except (TypeError, ValueError):
                page_no = 0
            asset = by_id.get(str(figure_id)) or by_page.get(page_no) or {}
            if not figure_id:
                figure_id = asset.get("id") or f"fig_{index + 1:03d}"
            if not page_no:
                try:
                    page_no = int(asset.get("page") or 0)
                except (TypeError, ValueError):
                    page_no = 0
            insights.append({
                "id": str(figure_id),
                "kind": asset.get("kind") or item.get("kind") or "page_snapshot",
                "label": text_value(item.get("label") or asset.get("label"), asset.get("label") or "关键页面"),
                "caption": text_value(item.get("caption") or asset.get("caption"), asset.get("caption") or MISSING_TEXT),
                "page": page_no or asset.get("page"),
                "image_path": asset.get("image_path") or item.get("image_path") or "",
                "confidence": asset.get("confidence", item.get("confidence", 0)),
                "title": text_value(item.get("title") or item.get("what_it_shows") or asset.get("reason"), "关键图表页"),
                "what_it_shows": text_value(item.get("what_it_shows") or item.get("summary") or item.get("what_to_look")),
                "how_to_read": text_value(item.get("how_to_read") or item.get("what_to_look")),
                "visual_elements": text_value(item.get("visual_elements")),
                "supports_claim": text_value(item.get("supports_claim") or item.get("what_it_proves")),
                "relation_to_method_or_experiment": text_value(item.get("relation_to_method_or_experiment") or item.get("why_important")),
                "misread_risks": text_value(item.get("misread_risks")),
                "page_refs": normalize_page_refs(item.get("page_refs") or [page_no or asset.get("page")]),
            })
    if insights:
        return insights
    return [_figure_from_asset(asset) for asset in figure_assets or [] if isinstance(asset, dict)]


def normalize_speedread_payload(parsed, figure_assets):
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
        "experiment_read": normalize_experiment_read(source.get("experiment_read")),
        "contributions": list_value(source.get("contributions"), max_items=6),
        "limitations": list_value(source.get("limitations"), max_items=6),
        "figure_insights": normalize_figure_insights(figure_source, figure_assets),
        "deep_read_suggestions": list_value(source.get("deep_read_suggestions"), max_items=8),
    }