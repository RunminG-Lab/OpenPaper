from __future__ import annotations

import re

from backend.utils import log

from .models import clean_page_text
from .pdf_extract import extract_page_layout_words, extract_page_visual_layout, render_page_image

CAPTION_ID_PATTERN = r"(?:\d+[A-Za-z0-9.\-]*|[IVXLCM]+[A-Za-z0-9.\-]*|[A-Za-z](?:\d+[A-Za-z0-9.\-]*)?)"
CAPTION_RE = re.compile(
    rf"(?im)^\s*((?:Figure|Fig\.?|Table|图|表)\s*{CAPTION_ID_PATTERN}\s*[:：.]\s*.{{0,260}})"
)
CAPTION_LINE_RE = re.compile(
    rf"^\s*((?:Figure|Fig\.?|Table|图|表)\s*{CAPTION_ID_PATTERN}\s*[:：.]\s*.{{0,260}})",
    re.I,
)
MAX_FIGURE_CANDIDATES = 12
FIGURE_EXTRACTOR_VERSION = "caption-safe-regions-v1"


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
    match = re.match(rf"\s*((?:Figure|Fig\.?|Table|图|表)\s*{CAPTION_ID_PATTERN})", caption or "", re.I)
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
    match = re.search(rf"(?:Figure|Fig\.?|Table|图|表)\s*({CAPTION_ID_PATTERN})", caption or "", re.I)
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


def _caption_column_words(segment, page_width, anchor_word=None):
    # `_split_word_segments` has already separated genuine columns at a large
    # horizontal gap.  A smaller but explicit gutter around the page midpoint
    # still separates two-column text; split only there and retain the side that
    # contains the caption label.  Full-width captions have normal word spacing
    # through the midpoint and therefore remain intact.
    if not segment or page_width <= 0 or anchor_word is None:
        return segment
    ordered = sorted(segment, key=lambda item: float(item.get("x0") or 0))
    spans_page_gutter = (
        float(ordered[0].get("x0") or 0) < page_width * 0.45
        and float(ordered[-1].get("x1") or 0) > page_width * 0.55
    )
    if not spans_page_gutter:
        return segment
    gutter_candidates = []
    for index, (previous, current) in enumerate(zip(ordered, ordered[1:]), start=1):
        previous_x1 = float(previous.get("x1") or 0)
        current_x0 = float(current.get("x0") or 0)
        gap = current_x0 - previous_x1
        crosses_gutter = previous_x1 <= page_width * 0.51 and current_x0 >= page_width * 0.49
        if crosses_gutter and gap >= max(6.0, page_width * 0.010):
            gutter_candidates.append((gap, index))
    if not gutter_candidates:
        return segment
    _gap, split_index = max(gutter_candidates)
    left, right = ordered[:split_index], ordered[split_index:]
    if anchor_word in left:
        return left
    if anchor_word in right:
        return right
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
            segment = _caption_column_words(segment, page_width, word)
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
    kind = _caption_kind(caption)
    expected_number = _caption_number(caption)
    label_tokens = {"table"} if kind == "table" else {"figure", "fig"}

    # A text block preserves the true caption width even when the first line is
    # short or the caption wraps.  Prefer it over reconstructing a single line.
    for block in layout.get("text_blocks") or []:
        block_text = str(block.get("text") or "").strip()
        block_height = float(block.get("y1") or 0) - float(block.get("y0") or 0)
        line_count = int(block.get("line_count") or 1)
        if line_count > 8 or block_height > float(layout.get("height") or 0) * 0.10:
            continue
        if not CAPTION_LINE_RE.match(block_text):
            continue
        if _caption_kind(block_text) != kind:
            continue
        if expected_number and _caption_number(block_text) != expected_number:
            continue
        if not _caption_text_matches(block_text, caption):
            continue
        result = {key: float(block.get(key) or 0) for key in ("x0", "y0", "x1", "y1")}
        result["source"] = "text_block"
        result["line_count"] = line_count
        return result

    if not words:
        return None

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
        segment = _caption_column_words(segment, float(layout.get("width") or 0), word)
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
            "source": "word_line",
            "line_count": 1,
        }
    return None


def _column_bounds_for_bbox(page_width, bbox, full_width=False):
    width = bbox["x1"] - bbox["x0"]
    center = (bbox["x0"] + bbox["x1"]) / 2
    crosses_center = bbox["x0"] < page_width * 0.35 and bbox["x1"] > page_width * 0.65
    centered_on_page = abs(center - page_width * 0.50) <= page_width * 0.05
    if full_width or width >= page_width * 0.52 or crosses_center or centered_on_page:
        return page_width * 0.06, page_width * 0.94
    if center < page_width * 0.50:
        return page_width * 0.06, page_width * 0.49
    return page_width * 0.51, page_width * 0.94


def _make_crop_rect(page_width, page_height, x0, y0, x1, y1, method="fallback", quality=0.5):
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
        "method": method,
        "quality": round(max(0.0, min(1.0, float(quality))), 3),
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


def _rect_width(rect):
    return max(0.0, float(rect.get("x1") or 0) - float(rect.get("x0") or 0))


def _rect_height(rect):
    return max(0.0, float(rect.get("y1") or 0) - float(rect.get("y0") or 0))


def _rect_union(rects):
    if not rects:
        return None
    return {
        "x0": min(float(rect.get("x0") or 0) for rect in rects),
        "y0": min(float(rect.get("y0") or 0) for rect in rects),
        "x1": max(float(rect.get("x1") or 0) for rect in rects),
        "y1": max(float(rect.get("y1") or 0) for rect in rects),
    }


def _axis_overlap(start_a, end_a, start_b, end_b):
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def _horizontal_overlap_ratio(first, second):
    overlap = _axis_overlap(first["x0"], first["x1"], second["x0"], second["x1"])
    return overlap / max(1.0, min(_rect_width(first), _rect_width(second)))


def _vertical_overlap_ratio(first, second):
    overlap = _axis_overlap(first["y0"], first["y1"], second["y0"], second["y1"])
    return overlap / max(1.0, min(_rect_height(first), _rect_height(second)))


def _rect_gap(first, second, axis):
    start = "x0" if axis == "x" else "y0"
    end = "x1" if axis == "x" else "y1"
    return max(0.0, max(float(first[start]), float(second[start])) - min(float(first[end]), float(second[end])))


def _word_rows_with_items(words, x0, x1, y_min, y_max):
    rows = []
    selected = []
    for word in words:
        word_x0 = float(word.get("x0") or 0)
        word_x1 = float(word.get("x1") or 0)
        word_y0 = float(word.get("y0") or 0)
        word_y1 = float(word.get("y1") or 0)
        center = (word_x0 + word_x1) / 2
        if center < x0 or center > x1 or word_y1 < y_min or word_y0 > y_max:
            continue
        selected.append(word)
    for word in sorted(selected, key=lambda item: (float(item.get("y0") or 0), float(item.get("x0") or 0))):
        word_y0 = float(word.get("y0") or 0)
        word_y1 = float(word.get("y1") or 0)
        if rows and abs(rows[-1]["y0"] - word_y0) <= 4:
            rows[-1]["y0"] = min(rows[-1]["y0"], word_y0)
            rows[-1]["y1"] = max(rows[-1]["y1"], word_y1)
            rows[-1]["words"].append(word)
        else:
            rows.append({"y0": word_y0, "y1": word_y1, "words": [word]})
    return rows


def _nearest_caption_barrier(layout, bbox, column_x0, column_x1, side):
    barriers = []
    for block in layout.get("text_blocks") or []:
        text = str(block.get("text") or "").strip()
        if not CAPTION_LINE_RE.match(text):
            continue
        block_y0 = float(block.get("y0") or 0)
        block_y1 = float(block.get("y1") or 0)
        if _axis_overlap(block_y0, block_y1, bbox["y0"], bbox["y1"]) > 0:
            continue
        block_x0 = float(block.get("x0") or 0)
        block_x1 = float(block.get("x1") or 0)
        overlap = _axis_overlap(block_x0, block_x1, column_x0, column_x1)
        if overlap / max(1.0, min(block_x1 - block_x0, column_x1 - column_x0)) < 0.25:
            continue
        if side == "below" and block_y0 > bbox["y1"] + 2:
            barriers.append(block_y0)
        elif side == "above" and block_y1 < bbox["y0"] - 2:
            barriers.append(block_y1)
    if not barriers:
        return None
    return min(barriers) if side == "below" else max(barriers)


def _table_rule_crop(layout, bbox, column_x0, column_x1):
    page_width = float(layout.get("width") or 0)
    page_height = float(layout.get("height") or 0)
    column_width = column_x1 - column_x0
    minimum_width = max(page_width * 0.25, column_width * 0.35)
    rules = []
    for rule in layout.get("horizontal_rules") or []:
        width = _rect_width(rule)
        overlap = _axis_overlap(rule["x0"], rule["x1"], column_x0, column_x1)
        if width < minimum_width or overlap / max(width, 1.0) < 0.60:
            continue
        rules.append(rule)
    if len(rules) < 2:
        return None

    x_groups = []
    for rule in sorted(rules, key=lambda item: (item["x0"], item["x1"], item["y0"])):
        matched_group = None
        for group in x_groups:
            anchor = group[0]
            endpoints_match = (
                abs(rule["x0"] - anchor["x0"]) <= page_width * 0.035
                and abs(rule["x1"] - anchor["x1"]) <= page_width * 0.035
            )
            width_ratio = min(_rect_width(rule), _rect_width(anchor)) / max(_rect_width(rule), _rect_width(anchor), 1.0)
            center_delta = abs((rule["x0"] + rule["x1"]) - (anchor["x0"] + anchor["x1"])) / 2
            near_equal_span = width_ratio >= 0.90 and center_delta <= page_width * 0.02
            if endpoints_match or near_equal_span:
                matched_group = group
                break
        if matched_group is None:
            x_groups.append([rule])
        else:
            matched_group.append(rule)

    candidates = []
    for x_group in x_groups:
        for side in ("above", "below"):
            barrier = _nearest_caption_barrier(layout, bbox, column_x0, column_x1, side)
            if side == "above":
                side_rules = [
                    rule for rule in x_group
                    if rule["y1"] <= bbox["y0"] + 2 and bbox["y0"] - rule["y1"] <= page_height * 0.48
                    and (barrier is None or rule["y0"] > barrier)
                ]
            else:
                side_rules = [
                    rule for rule in x_group
                    if rule["y0"] >= bbox["y1"] - 2 and rule["y0"] - bbox["y1"] <= page_height * 0.48
                    and (barrier is None or rule["y1"] < barrier)
                ]
            if len(side_rules) < 2:
                continue
            side_rules.sort(key=lambda item: item["y0"])
            # Long tables often have rules only at the header and the bottom;
            # splitting by vertical distance turns the header into a plausible
            # but incomplete mini-table.  A following/preceding caption is the
            # safer boundary between distinct same-column tables.
            for rule_group in (side_rules,):
                if len(rule_group) < 2:
                    continue
                region = _rect_union(rule_group)
                region_height = _rect_height(region)
                rows = _line_rows_in_region(
                    layout.get("words") or [], region["x0"], region["x1"], region["y0"] - 2, region["y1"] + 2
                )
                has_complete_content = len(rows) >= 2 or (len(rule_group) >= 3 and region_height >= page_height * 0.06)
                if region_height < page_height * 0.025 or not has_complete_content:
                    continue
                gap = bbox["y0"] - region["y1"] if side == "above" else region["y0"] - bbox["y1"]
                if gap > page_height * 0.13:
                    continue
                score = (len(rule_group) * 0.20) + (region_height / page_height * 2.0) - (max(gap, 0) / page_height)
                candidates.append((score, region))
    if not candidates:
        return None
    _score, region = max(candidates, key=lambda item: item[0])
    pad_x = page_width * 0.014
    pad_y = page_height * 0.006
    return _make_crop_rect(
        page_width,
        page_height,
        region["x0"] - pad_x,
        region["y0"] - pad_y,
        region["x1"] + pad_x,
        region["y1"] + pad_y,
        method="table_rules",
        quality=0.96,
    )


def _row_has_table_gap(row, column_width):
    words = sorted(row.get("words") or [], key=lambda item: float(item.get("x0") or 0))
    if len(words) < 2:
        return False
    threshold = max(14.0, column_width * 0.035)
    return any(float(current.get("x0") or 0) - float(previous.get("x1") or 0) >= threshold for previous, current in zip(words, words[1:]))


def _table_text_crop(layout, bbox, column_x0, column_x1):
    page_width = float(layout.get("width") or 0)
    page_height = float(layout.get("height") or 0)
    column_width = column_x1 - column_x0
    candidates = []
    for side in ("above", "below"):
        if side == "above":
            rows = _word_rows_with_items(layout.get("words") or [], column_x0, column_x1, page_height * 0.04, bbox["y0"] - 2)
            rows = list(reversed(rows))
            edge_gap = (bbox["y0"] - rows[0]["y1"]) if rows else page_height
        else:
            rows = _word_rows_with_items(layout.get("words") or [], column_x0, column_x1, bbox["y1"] + 2, page_height * 0.96)
            edge_gap = (rows[0]["y0"] - bbox["y1"]) if rows else page_height
        if not rows or edge_gap > page_height * 0.10:
            continue
        group = [rows[0]]
        for row in rows[1:]:
            if side == "above":
                gap = group[-1]["y0"] - row["y1"]
            else:
                gap = row["y0"] - group[-1]["y1"]
            if gap > 18:
                break
            group.append(row)
        qualifying = [row for row in group if _row_has_table_gap(row, column_width)]
        if len(group) < 2 or len(qualifying) < 2:
            continue
        all_words = [word for row in group for word in row.get("words") or []]
        region = {
            "x0": min(float(word.get("x0") or 0) for word in all_words),
            "y0": min(float(row["y0"]) for row in group),
            "x1": max(float(word.get("x1") or 0) for word in all_words),
            "y1": max(float(row["y1"]) for row in group),
        }
        if _rect_width(region) < page_width * 0.20 or _rect_height(region) < page_height * 0.025:
            continue
        score = len(qualifying) + _rect_height(region) / page_height - edge_gap / page_height
        candidates.append((score, region))
    if not candidates:
        return None
    _score, region = max(candidates, key=lambda item: item[0])
    return _make_crop_rect(
        page_width,
        page_height,
        region["x0"] - page_width * 0.014,
        region["y0"] - page_height * 0.010,
        region["x1"] + page_width * 0.014,
        region["y1"] + page_height * 0.010,
        method="table_text_rows",
        quality=0.68,
    )


def _visual_rects_connect(first, second, page_width, page_height):
    if _horizontal_overlap_ratio(first, second) >= 0.20 and _rect_gap(first, second, "y") <= page_height * 0.03:
        return True
    if _vertical_overlap_ratio(first, second) >= 0.20 and _rect_gap(first, second, "x") <= page_width * 0.025:
        return True
    return False


def _merge_visual_rects(rects, page_width, page_height):
    parents = list(range(len(rects)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left_index, first in enumerate(rects):
        for right_index in range(left_index + 1, len(rects)):
            if _visual_rects_connect(first, rects[right_index], page_width, page_height):
                union(left_index, right_index)

    groups = {}
    for index, rect in enumerate(rects):
        groups.setdefault(find(index), []).append(rect)
    return [_rect_union(group) for group in groups.values()]


def _rect_crosses_other_caption(layout, rect, current_bbox, column_x0, column_x1):
    for block in layout.get("text_blocks") or []:
        text = str(block.get("text") or "").strip()
        if not CAPTION_LINE_RE.match(text):
            continue
        block_y0 = float(block.get("y0") or 0)
        block_y1 = float(block.get("y1") or 0)
        if _axis_overlap(block_y0, block_y1, current_bbox["y0"], current_bbox["y1"]) > 0:
            continue
        if _axis_overlap(block_y0, block_y1, rect["y0"], rect["y1"]) <= 0:
            continue
        block_x0 = float(block.get("x0") or 0)
        block_x1 = float(block.get("x1") or 0)
        if _axis_overlap(block_x0, block_x1, column_x0, column_x1) > 0:
            return True
    return False


def _figure_visual_crop(layout, bbox, column_x0, column_x1):
    page_width = float(layout.get("width") or 0)
    page_height = float(layout.get("height") or 0)
    page_area = page_width * page_height
    source_rects = []
    for rect in (layout.get("image_bboxes") or []) + (layout.get("drawing_rects") or []):
        width = _rect_width(rect)
        height = _rect_height(rect)
        area = width * height
        if width < page_width * 0.04 or height < page_height * 0.012:
            continue
        if area < page_area * 0.0008:
            continue
        horizontal_overlap = _axis_overlap(rect["x0"], rect["x1"], column_x0, column_x1)
        if horizontal_overlap / max(width, 1.0) < 0.45:
            continue
        source_rects.append(rect)
    if not source_rects:
        return None

    candidates = list(source_rects)
    candidates.extend(_merge_visual_rects(source_rects, page_width, page_height))
    scored = []
    for raw_rect in candidates:
        if _rect_crosses_other_caption(layout, raw_rect, bbox, column_x0, column_x1):
            continue
        rect = dict(raw_rect)
        if rect["y0"] >= bbox["y0"]:
            continue
        if rect["y1"] > bbox["y0"]:
            if rect["y1"] > bbox["y1"] + page_height * 0.025:
                continue
            # Some PDF drawing groups include the caption baseline in an
            # otherwise complete enclosing rectangle.  Keep the enclosing
            # width but clip its lower edge immediately above the caption.
            rect["y1"] = bbox["y0"] - page_height * 0.006
        width = _rect_width(rect)
        height = _rect_height(rect)
        area = width * height
        if width < page_width * 0.20 or height < page_height * 0.06 or area < page_area * 0.02:
            continue
        gap = bbox["y0"] - rect["y1"]
        if gap > page_height * 0.16:
            continue
        score = (area / page_area * 8.0) + (width / page_width) + (height / page_height) - (max(gap, 0) / page_height * 4.0)
        scored.append((gap, score, rect))
    # Scientific figure captions are conventionally below the figure.  In a
    # broad local corpus, below-caption candidates were consistently the next
    # numbered figure/table on the page.  If visual grouping cannot prove an
    # above-caption region, the conservative caption window is safer than
    # attaching a later image.
    if not scored:
        return None
    minimum_gap = min(item[0] for item in scored)
    nearby_pool = [item for item in scored if item[0] <= minimum_gap + max(8.0, page_height * 0.03)]
    _gap, _score, region = max(nearby_pool, key=lambda item: item[1])
    return _make_crop_rect(
        page_width,
        page_height,
        region["x0"] - page_width * 0.010,
        region["y0"] - page_height * 0.006,
        region["x1"] + page_width * 0.010,
        region["y1"] + page_height * 0.006,
        method="visual_region",
        quality=0.94,
    )


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
    if not str(layout.get("source") or "").startswith("pymupdf"):
        try:
            visual_layout = extract_page_visual_layout(pdf_path, page_no)
        except Exception as exc:
            log(f"读取第 {page_no} 页视觉布局失败，使用文本回退: {exc}")
            visual_layout = {}
        if visual_layout.get("width") and visual_layout.get("height"):
            base_width = float(layout.get("width") or 0)
            base_height = float(layout.get("height") or 0)
            visual_width = float(visual_layout.get("width") or 0)
            visual_height = float(visual_layout.get("height") or 0)
            dimensions_align = (
                abs(base_width - visual_width) <= max(2.0, visual_width * 0.02)
                and abs(base_height - visual_height) <= max(2.0, visual_height * 0.02)
                and not int(visual_layout.get("rotation") or 0)
            )
            if dimensions_align and layout.get("words"):
                # Keep Poppler words for fonts PyMuPDF cannot decode, while
                # overlaying visual geometry from the renderer's coordinate
                # system.  Rotated pages intentionally use the all-PyMuPDF
                # layout because Poppler reports rotated coordinates.
                visual_layout["words"] = layout.get("words") or []
                visual_layout["source"] = "pymupdf+poppler"
            layout = visual_layout
            layout_cache[page_no] = visual_layout
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
        # Lines and repeated text rows prove that a table is complete.  If we
        # cannot establish that, skip the asset instead of showing an arbitrary
        # caption-adjacent strip as a high-confidence table.
        return _table_rule_crop(layout, bbox, x0, x1) or _table_text_crop(layout, bbox, x0, x1)

    visual_crop = _figure_visual_crop(layout, bbox, x0, x1)
    if visual_crop:
        return visual_crop

    # A line beginning with "Figure N" inside body prose is a common inline
    # reference.  Without either a real caption block or nearby visual geometry
    # there is not enough evidence to crop safely.
    if bbox.get("source") != "text_block" or int(bbox.get("line_count") or 1) > 4:
        return None

    # Conservative fallback for scanned PDFs or diagrams without extractable
    # vector/image geometry.  It keeps more context, but uses lower quality so
    # the UI never presents it as a precise crop.
    if bbox["y0"] > page_height * 0.12:
        y1 = bbox["y0"] - 5
        y0 = max(page_height * 0.04, y1 - page_height * 0.46)
    else:
        y0 = min(page_height * 0.18, bbox["y1"] + 6)
        y1 = min(page_height * 0.72, y0 + page_height * 0.46)
    return _make_crop_rect(page_width, page_height, x0, y0, x1, y1, method="caption_window", quality=0.45)


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
    def __init__(self, workspace_root, quick_reading_cache_dir, image_width=3200):
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
            crop_quality = float(crop_rect.get("quality") or 0.5)
            confidence = min(crop_quality, min(0.95, max(0.35, raw_score / 16)))
            assets.append({
                "id": asset_id,
                "kind": "figure_crop",
                "label": label,
                "caption": caption or "论文中未明确说明",
                "page": page_no,
                "image_path": image_path,
                "confidence": round(confidence, 2),
                "crop_rect": {
                    key: crop_rect.get(key)
                    for key in ("x0", "y0", "x1", "y1", "page_width", "page_height", "method", "quality")
                },
                "reason": item.get("reason") or "图表/实验候选页",
                "excerpt": item.get("excerpt") or "",
            })
        assets.sort(key=lambda item: item.get("page") or 0)
        return assets
