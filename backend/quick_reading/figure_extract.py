from __future__ import annotations

import re

from backend.utils import log

from .models import clean_page_text
from .pdf_extract import render_page_image, select_important_pages

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
            ranked.append({
                "page": page_no,
                "score": score,
                "captions": captions,
                "excerpt": clean_page_text(text, 900),
            })

        ranked.sort(key=lambda item: (-item["score"], item["page"]))
        selected = []
        used_pages = set()
        for item in ranked:
            if item["page"] in used_pages:
                continue
            selected.append(item)
            used_pages.add(item["page"])
            if len(selected) >= requested:
                break

        if len(selected) < requested:
            for fallback in select_important_pages(page_texts, page_count, requested):
                page_no = fallback.get("page")
                if page_no in used_pages:
                    continue
                text = page_texts[page_no - 1] if page_no and page_no - 1 < page_count else ""
                selected.append({
                    "page": page_no,
                    "score": 1,
                    "captions": _extract_captions(text),
                    "excerpt": fallback.get("excerpt") or clean_page_text(text, 900),
                    "reason": fallback.get("reason", "补充页面"),
                })
                used_pages.add(page_no)
                if len(selected) >= requested:
                    break

        assets = []
        for asset_index, item in enumerate(selected[:requested], start=1):
            page_no = int(item.get("page") or 0)
            captions = item.get("captions") or []
            caption = captions[0] if captions else ""
            label = _caption_label(caption, page_no)
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
                )
            except Exception as exc:
                log(f"速读图表页渲染失败 page={page_no}: {exc}")
            raw_score = int(item.get("score") or 0)
            confidence = min(0.95, max(0.35, raw_score / 16))
            assets.append({
                "id": asset_id,
                "kind": "page_snapshot",
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