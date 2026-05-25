from __future__ import annotations

import re

from backend.utils import log

from .models import clean_page_text
from .pdf_extract import extract_page_layout_words, render_page_image

CAPTION_RE = re.compile(
    r"(?im)^\s*((?:Figure|Fig\.?|Table|图|表)\s*[\dIVXLC]+[A-Za-z0-9.\-]*?\s*[:：.]\s*.{0,260})"
)
CAPTION_LINE_RE = re.compile(
    r"^\s*((?:Figure|Fig\.?|Table|图|表)\s*[\dIVXLC]+[A-Za-z0-9.\-]*?\s*[:：.]\s*.{0,260})",
    re.I,
)
MAX_FIGURE_CANDIDATES = 12


def _extract_captions(text):
    captions = []
    for match in CAPTION_RE.finditer(text or ""):
        caption = clean_page_text(match.group(1), 320)
        if len(_caption_match_tokens(caption)) < 2:
            continue
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


def _label_number_matches(words, index, expected_number):
    if not expected_number:
        return True
    lookahead = [_clean_label_token(item.get("text")) for item in words[index + 1:index + 4]]
    if expected_number.isdigit():
        return any(token == expected_number or re.match(rf"^{re.escape(expected_number)}[a-z]?$", token) for token in lookahead)
    return any(token == expected_number for token in lookahead)


def _caption_match_tokens(value):
    tokens = re.findall(r"[a-z0-9]+", (value or "").lower())
    ignored = {"figure", "fig", "table", "the", "and", "for", "with", "from", "left", "right", "page"}
    return [token for token in tokens if token not in ignored and not re.fullmatch(r"[ivxlcdm]+|\d+", token)]


def _caption_text_matches(candidate, caption):
    candidate_tokens = _caption_match_tokens(candidate)
    caption_tokens = _caption_match_tokens(caption)
    if len(caption_tokens) < 2:
        return True
    required = caption_tokens[: min(5, len(caption_tokens))]
    matched = sum(1 for token in required if token in candidate_tokens)
    return matched >= min(2, len(required))


def _split_word_segments(line_words):
    segments = []
    current = []
    previous_x1 = None
    for item in sorted(line_words, key=lambda word: float(word.get("x0") or 0)):
        x0 = float(item.get("x0") or 0)
        if current and previous_x1 is not None and x0 - previous_x1 > 28:
            segments.append(current)
            current = []
        current.append(item)
        previous_x1 = float(item.get("x1") or x0)
    if current:
        segments.append(current)
    return segments


def _caption_column_words(segment, page_width):
    if not segment or page_width <= 0:
        return segment
    x0 = min(float(item.get("x0") or 0) for item in segment)
    x1 = max(float(item.get("x1") or 0) for item in segment)
    if x0 < page_width * 0.25 and x1 > page_width * 0.70:
        return [item for item in segment if float(item.get("x1") or 0) <= page_width * 0.50] or segment
    if x0 < page_width * 0.30 and x1 > page_width * 0.75:
        return [item for item in segment if float(item.get("x0") or 0) >= page_width * 0.50] or segment
    return segment


def _line_words_for_anchor(words, anchor_y, anchor_height):
    tolerance = max(anchor_height * 0.75, 4)
    return [item for item in words if abs(float(item.get("y0") or 0) - anchor_y) <= tolerance]


def _extract_captions_from_layout(layout):
    words = layout.get("words") or []
    page_width = float(layout.get("width") or 0)
    captions = []
    for word in words:
        anchor_y = float(word.get("y0") or 0)
        anchor_height = max(float(word.get("y1") or 0) - anchor_y, 3)
        for segment in _split_word_segments(_line_words_for_anchor(words, anchor_y, anchor_height)):
            segment = _caption_column_words(segment, page_width)
            text = " ".join(str(item.get("text") or "") for item in segment).strip()
            match = CAPTION_LINE_RE.match(text)
            if not match:
                continue
            caption = clean_page_text(match.group(1), 320)
            if len(_caption_match_tokens(caption)) < 2:
                continue
            if caption and caption not in captions:
                captions.append(caption)
            if len(captions) >= 3:
                return captions
    return captions


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
        if not _label_number_matches(words, index, expected_number):
            continue
        anchor_y = float(word.get("y0") or 0)
        anchor_height = max(float(word.get("y1") or 0) - anchor_y, 3)
        line_words = _line_words_for_anchor(words, anchor_y, anchor_height)
        segment = next((items for items in _split_word_segments(line_words) if word in items), [])
        if not segment:
            continue
        segment = _caption_column_words(segment, float(layout.get("width") or 0))
        segment_text = " ".join(str(item.get("text") or "") for item in segment).strip()
        if not CAPTION_LINE_RE.match(segment_text):
            continue
        if not _caption_text_matches(segment_text, caption):
            continue
        return {
            "x0": min(float(item.get("x0") or 0) for item in segment),
            "y0": min(float(item.get("y0") or 0) for item in segment),
            "x1": max(float(item.get("x1") or 0) for item in segment),
            "y1": max(float(item.get("y1") or 0) for item in segment),
        }
    return None


def _column_bounds_for_bbox(page_width, bbox, full_width=False):
    if full_width or bbox["x1"] - bbox["x0"] > page_width * 0.55:
        return page_width * 0.06, page_width * 0.94
    if bbox["x0"] < page_width * 0.50:
        return page_width * 0.06, page_width * 0.49
    return page_width * 0.51, page_width * 0.94


def _make_crop_rect(page_width, page_height, x0, y0, x1, y1):
    x0 = max(0, min(page_width, x0))
    x1 = max(0, min(page_width, x1))
    y0 = max(0, min(page_height, y0))
    y1 = max(0, min(page_height, y1))
    if x1 - x0 < page_width * 0.18 or y1 - y0 < page_height * 0.05:
        return None
    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "page_width": page_width,
        "page_height": page_height,
    }


def _line_rows_in_region(words, x0, x1, y_min, y_max):
    rows = []
    for word in sorted(words, key=lambda item: float(item.get("y0") or 0)):
        word_x0 = float(word.get("x0") or 0)
        word_x1 = float(word.get("x1") or 0)
        word_y0 = float(word.get("y0") or 0)
        word_y1 = float(word.get("y1") or 0)
        word_center = (word_x0 + word_x1) / 2
        if word_center < x0 or word_center > x1 or word_y1 < y_min or word_y0 > y_max:
            continue
        if rows and abs(rows[-1]["y0"] - word_y0) <= 4:
            rows[-1]["y0"] = min(rows[-1]["y0"], word_y0)
            rows[-1]["y1"] = max(rows[-1]["y1"], word_y1)
            continue
        rows.append({"y0": word_y0, "y1": word_y1})
    return rows


def _table_body_bottom(layout, bbox, x0, x1, page_height):
    rows = _line_rows_in_region(layout.get("words") or [], x0, x1, bbox["y1"] + 2, page_height * 0.96)
    if not rows or rows[0]["y0"] - bbox["y1"] > page_height * 0.10:
        return None
    last = rows[0]
    for row in rows[1:]:
        if row["y0"] - last["y1"] > 18:
            break
        last = row
    return min(page_height * 0.96, last["y1"] + 10)


def _table_body_above(layout, bbox, x0, x1, page_height):
    rows = _line_rows_in_region(layout.get("words") or [], x0, x1, page_height * 0.04, bbox["y0"] - 2)
    if not rows or bbox["y0"] - rows[-1]["y1"] > page_height * 0.09:
        return None
    first = rows[-1]
    for row in reversed(rows[:-1]):
        if first["y0"] - row["y1"] > 18:
            break
        first = row
    return max(page_height * 0.04, first["y0"] - 10)


def _crop_signature(crop_rect):
    if not crop_rect:
        return None
    return tuple(round(float(crop_rect.get(key) or 0), 1) for key in ("x0", "y0", "x1", "y1"))


def _crop_rect_for_candidate(pdf_path, page_no, caption, layout_cache):
    if not page_no:
        return None
    if not caption:
        return None
    if page_no not in layout_cache:
        layout_cache[page_no] = extract_page_layout_words(pdf_path, page_no)
    layout = layout_cache.get(page_no) or {}
    page_width = float(layout.get("width") or 0)
    page_height = float(layout.get("height") or 0)
    if page_width <= 0 or page_height <= 0:
        return None

    bbox = _find_caption_bbox(layout, caption)
    if not bbox:
        return None
    is_table = _caption_kind(caption) == "table"
    x0, x1 = _column_bounds_for_bbox(page_width, bbox)

    if is_table:
        y0 = _table_body_above(layout, bbox, x0, x1, page_height)
        if y0:
            y1 = min(page_height * 0.96, bbox["y1"] + 8)
        else:
            y0 = min(page_height * 0.96, bbox["y1"] + 10)
            y1 = _table_body_bottom(layout, bbox, x0, x1, page_height)
            if not y1:
                y1 = min(page_height * 0.96, y0 + page_height * 0.34)
    else:
        y1 = bbox["y0"] - 8
        if y1 > page_height * 0.06:
            window = 0.34 if bbox["y0"] < page_height * 0.45 else 0.25
            y0 = max(page_height * 0.04, y1 - page_height * window)
        else:
            y0 = min(page_height * 0.18, bbox["y1"] + 6)
            y1 = min(page_height * 0.72, y0 + page_height * 0.42)
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
        requested = max(0, min(MAX_FIGURE_CANDIDATES, requested))
        if requested <= 0:
            return []

        page_count = len(page_texts or [])
        ranked = []
        layout_cache = {}
        for page_index in range(page_count):
            page_no = page_index + 1
            text = page_texts[page_index] or ""
            layout_cache[page_no] = extract_page_layout_words(pdf_path, page_no)
            captions = _extract_captions_from_layout(layout_cache[page_no]) or _extract_captions(text)
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
            if len(selected) >= requested * 2:
                break

        assets = []
        used_crop_signatures = set()
        selected_assets = sorted(selected, key=lambda item: (item.get("page") or 0, item.get("label") or ""))
        for item in selected_assets:
            if len(assets) >= requested:
                break
            page_no = int(item.get("page") or 0)
            caption = item.get("caption") or ""
            label = item.get("label") or _caption_label(caption, page_no)
            crop_rect = _crop_rect_for_candidate(pdf_path, page_no, caption, layout_cache)
            if not crop_rect:
                continue
            crop_key = (page_no, _crop_signature(crop_rect))
            if crop_key in used_crop_signatures:
                continue
            used_crop_signatures.add(crop_key)
            asset_id = f"fig_{len(assets) + 1:03d}"
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
            if not image_path:
                continue
            raw_score = int(item.get("score") or 0)
            confidence = min(0.95, max(0.35, raw_score / 16))
            assets.append({
                "id": asset_id,
                "kind": "figure_crop",
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
