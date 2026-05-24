from __future__ import annotations

import re

from backend.utils import log

from .models import clean_page_text
from .pdf_extract import extract_page_layout_words, render_page_image, select_important_pages

CAPTION_RE = re.compile(
    r"(?im)^\s*((?:Figure|Fig\.?|Table|图|表)\s*[\dIVXLC]+[A-Za-z0-9.\-]*\s*[:：.]?\s*.{0,260})"
)


def _extract_captions(text):
    captions = []
    for match in CAPTION_RE.finditer(text or ""):
        caption = clean_page_text(match.group(1), 320)
        if caption and caption not in captions:
            captions.append(caption)
        if len(captions) >= 3:
            break
    return captions


def _caption_label(caption, page_no):
    match = re.match(r"\s*((?:Figure|Fig\.?|Table|图|表)\s*[\dIVXLC]+[A-Za-z0-9.\-]*)", caption or "", re.I)
    if match:
        return match.group(1).strip()
    return f"Page {page_no}"


def _caption_kind(caption):
    lowered = (caption or "").strip().lower()
    if lowered.startswith("table") or lowered.startswith("表"):
        return "table"
    return "figure"


def _clean_label_token(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _caption_number(caption):
    match = re.search(r"(?:Figure|Fig\.?|Table|图|表)\s*([\dIVXLC]+)", caption or "", re.I)
    return _clean_label_token(match.group(1)) if match else ""


def _find_caption_bbox(layout, caption):
    words = layout.get("words") or []
    if not words:
        return None
    kind = _caption_kind(caption)
    expected_number = _caption_number(caption)
    label_tokens = {"table"} if kind == "table" else {"figure", "fig"}

    for index, word in enumerate(words):
        token = _clean_label_token(word.get("text"))
        is_chinese_label = word.get("text", "").startswith("表" if kind == "table" else "图")
        if token not in label_tokens and not is_chinese_label:
            continue
        if expected_number:
            lookahead = "".join(_clean_label_token(item.get("text")) for item in words[index + 1:index + 4])
            if expected_number not in lookahead[: max(len(expected_number) + 2, 2)]:
                continue
        anchor_y = float(word.get("y0") or 0)
        anchor_height = max(float(word.get("y1") or 0) - anchor_y, 3)
        tolerance = max(anchor_height * 0.75, 4)
        line_words = [item for item in words if abs(float(item.get("y0") or 0) - anchor_y) <= tolerance]
        if not line_words:
            continue
        return {
            "x0": min(float(item.get("x0") or 0) for item in line_words),
            "y0": min(float(item.get("y0") or 0) for item in line_words),
            "x1": max(float(item.get("x1") or 0) for item in line_words),
            "y1": max(float(item.get("y1") or 0) for item in line_words),
        }
    return None


def _make_crop_rect(page_width, page_height, x0, y0, x1, y1):
    x0 = max(0, min(page_width, x0))
    x1 = max(0, min(page_width, x1))
    y0 = max(0, min(page_height, y0))
    y1 = max(0, min(page_height, y1))
    if x1 - x0 < page_width * 0.18 or y1 - y0 < page_height * 0.10:
        return None
    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "page_width": page_width,
        "page_height": page_height,
    }


def _crop_rect_for_candidate(pdf_path, page_no, caption, layout_cache):
    if not page_no:
        return None
    if page_no not in layout_cache:
        layout_cache[page_no] = extract_page_layout_words(pdf_path, page_no)
    layout = layout_cache.get(page_no) or {}
    page_width = float(layout.get("width") or 0)
    page_height = float(layout.get("height") or 0)
    if page_width <= 0 or page_height <= 0:
        return None

    bbox = _find_caption_bbox(layout, caption) if caption else None
    x0 = page_width * 0.06
    x1 = page_width * 0.94
    if not bbox:
        return _make_crop_rect(page_width, page_height, x0, page_height * 0.08, x1, page_height * 0.58)

    if _caption_kind(caption) == "table":
        y0 = max(page_height * 0.04, bbox["y0"] - 8)
        y1 = min(page_height * 0.96, y0 + page_height * 0.36)
    else:
        y1 = bbox["y0"] - 8
        if y1 < page_height * 0.18:
            y0 = min(page_height * 0.18, bbox["y1"] + 6)
            y1 = min(page_height * 0.72, y0 + page_height * 0.42)
        else:
            y0 = max(page_height * 0.04, y1 - page_height * 0.42)
    return _make_crop_rect(page_width, page_height, x0, y0, x1, y1)


def _score_candidate_page(page_no, page_count, text, captions):
    lower = (text or "").lower()
    score = 0
    if captions:
        score += 6
    priority_terms = [
        "framework", "pipeline", "architecture", "overview", "method", "approach",
        "comparison", "baseline", "ablation", "variant", "result", "results", "evaluation",
        "benchmark", "dataset", "metric", "qualitative", "case study", "limitation", "discussion",
    ]
    for term in priority_terms:
        if term in lower:
            score += 2
    if "table" in lower or "figure" in lower or "fig." in lower or "图" in lower or "表" in lower:
        score += 3
    if page_no <= 2:
        score += 1
    if page_count and page_no >= max(page_count - 1, 1) and ("limitation" in lower or "discussion" in lower):
        score += 1
    return score


class FigureExtractionService:
    def __init__(self, workspace_root, quick_reading_cache_dir, image_width=1400):
        self.workspace_root = workspace_root
        self.quick_reading_cache_dir = quick_reading_cache_dir
        self.image_width = image_width

    def extract_important_figures(self, paper_id, pdf_path, page_texts, max_figures):
        try:
            requested = int(max_figures)
        except (TypeError, ValueError):
            requested = 5
        requested = max(0, min(8, requested))
        if requested <= 0:
            return []

        page_count = len(page_texts or [])
        ranked = []
        for page_index in range(page_count):
            page_no = page_index + 1
            text = page_texts[page_index] or ""
            captions = _extract_captions(text)
            score = _score_candidate_page(page_no, page_count, text, captions)
            if score <= 0 and not text.strip():
                continue
            excerpt = clean_page_text(text, 900)
            if captions:
                for caption_index, caption in enumerate(captions):
                    ranked.append({
                        "page": page_no,
                        "score": score - caption_index,
                        "has_caption": True,
                        "caption": caption,
                        "label": _caption_label(caption, page_no),
                        "excerpt": excerpt,
                        "reason": "图表 caption 候选",
                    })
            else:
                ranked.append({
                    "page": page_no,
                    "score": score,
                    "has_caption": False,
                    "caption": "",
                    "label": f"Page {page_no}",
                    "excerpt": excerpt,
                    "reason": "关键页面候选",
                })

        ranked.sort(key=lambda item: (0 if item.get("has_caption") else 1, -item["score"], item["page"], item.get("label", "")))
        selected = []
        used_keys = set()
        used_pages = set()
        for item in ranked:
            key = (item["page"], (item.get("label") or item.get("caption") or "").lower())
            if key in used_keys:
                continue
            selected.append(item)
            used_keys.add(key)
            used_pages.add(item["page"])
            if len(selected) >= requested:
                break

        if len(selected) < requested:
            fallback_limit = max(requested * 2, requested + len(selected))
            for fallback in select_important_pages(page_texts, page_count, fallback_limit):
                page_no = fallback.get("page")
                if not page_no or page_no in used_pages:
                    continue
                text = page_texts[page_no - 1] if page_no and page_no - 1 < page_count else ""
                caption = ""
                captions = _extract_captions(text)
                if captions:
                    caption = captions[0]
                selected.append({
                    "page": page_no,
                    "score": 1,
                    "has_caption": bool(caption),
                    "caption": caption,
                    "label": _caption_label(caption, page_no),
                    "excerpt": fallback.get("excerpt") or clean_page_text(text, 900),
                    "reason": fallback.get("reason", "补充页面"),
                })
                used_pages.add(page_no)
                if len(selected) >= requested:
                    break

        assets = []
        layout_cache = {}
        selected_assets = sorted(selected[:requested], key=lambda item: (item.get("page") or 0, item.get("label") or ""))
        for asset_index, item in enumerate(selected_assets, start=1):
            page_no = int(item.get("page") or 0)
            caption = item.get("caption") or ""
            label = item.get("label") or _caption_label(caption, page_no)
            crop_rect = _crop_rect_for_candidate(pdf_path, page_no, caption, layout_cache)
            asset_id = f"fig_{asset_index:03d}"
            image_path = ""
            try:
                image_path = render_page_image(
                    pdf_path,
                    paper_id,
                    page_no,
                    self.workspace_root,
                    self.quick_reading_cache_dir,
                    self.image_width,
                    base_name=f"{asset_id}_p{page_no:03d}",
                    crop_rect=crop_rect,
                )
            except Exception as exc:
                log(f"速读图表页渲染失败 page={page_no}: {exc}")
            raw_score = int(item.get("score") or 0)
            confidence = min(0.95, max(0.35, raw_score / 16))
            assets.append({
                "id": asset_id,
                "kind": "figure_crop" if crop_rect else "page_snapshot",
                "label": label,
                "caption": caption or "论文中未明确说明",
                "page": page_no,
                "image_path": image_path,
                "confidence": round(confidence, 2),
                "reason": item.get("reason") or "图表/实验候选页",
                "excerpt": item.get("excerpt") or "",
            })
        assets.sort(key=lambda item: item.get("page") or 0)
        return assets
