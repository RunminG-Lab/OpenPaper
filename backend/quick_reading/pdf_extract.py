from __future__ import annotations

import html
import os
import re
import subprocess
from urllib.parse import unquote

from backend.metadata import load_metadata
from backend.utils import safe_rel

from .models import clean_page_text
from .storage import paper_cache_dir

try:
    import fitz
except ImportError:
    fitz = None

try:
    from PIL import Image
except ImportError:
    Image = None

PAGE_SIZE_RE = re.compile(r"<page\b[^>]*\bwidth=\"([0-9.]+)\"[^>]*\bheight=\"([0-9.]+)\"", re.I)
WORD_BOX_RE = re.compile(
    r"<word\b[^>]*\bxMin=\"([0-9.]+)\"[^>]*\byMin=\"([0-9.]+)\""
    r"[^>]*\bxMax=\"([0-9.]+)\"[^>]*\byMax=\"([0-9.]+)\"[^>]*>(.*?)</word>",
    re.I | re.S,
)


def resolve_pdf_entry(file_key, metadata, workspace_root):
    entry = metadata.get(file_key)
    if not isinstance(entry, dict):
        return None, None, None
    pdf_local = entry.get("pdf_local") or entry.get("pdf") or ""
    rel_disk = unquote(pdf_local)
    abs_pdf = safe_rel(rel_disk, workspace_root)
    if not abs_pdf or not os.path.isfile(abs_pdf):
        return entry, rel_disk, None
    return entry, rel_disk.replace("\\", "/"), abs_pdf


def load_metadata_for_generation(metadata_file):
    return load_metadata(metadata_file)


def run_capture(cmd):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return None


def open_pdf_doc(abs_pdf):
    if fitz is None:
        raise RuntimeError("缺少 PDF 处理组件：未找到 pdfinfo/pdftotext/pdftoppm，且 PyMuPDF 不可用")
    try:
        return fitz.open(abs_pdf)
    except Exception as exc:
        raise RuntimeError(f"打开 PDF 失败: {exc}") from exc


def read_pdf_page_count(abs_pdf):
    result = run_capture(["pdfinfo", abs_pdf])
    if result is not None and result.returncode == 0:
        match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.IGNORECASE | re.MULTILINE)
        if match:
            return int(match.group(1))
    doc = open_pdf_doc(abs_pdf)
    try:
        return len(doc)
    finally:
        doc.close()


def extract_pdf_pages_text(abs_pdf, page_count):
    result = run_capture(["pdftotext", "-layout", "-enc", "UTF-8", abs_pdf, "-"])
    if result is not None and result.returncode == 0:
        raw_pages = result.stdout.split("\f")
        pages = [clean_page_text(page) for page in raw_pages]
    else:
        doc = open_pdf_doc(abs_pdf)
        try:
            pages = [clean_page_text(page.get_text("text")) for page in doc]
        finally:
            doc.close()
    while pages and not pages[-1]:
        pages.pop()
    if page_count and len(pages) > page_count:
        pages = pages[:page_count]
    if page_count and len(pages) < page_count:
        pages.extend([""] * (page_count - len(pages)))
    return pages


def extract_page_layout_words(abs_pdf, page_no):
    result = run_capture([
        "pdftotext",
        "-bbox-layout",
        "-enc", "UTF-8",
        "-f", str(page_no),
        "-l", str(page_no),
        abs_pdf,
        "-",
    ])
    if result is None or result.returncode != 0:
        return {"width": 0, "height": 0, "words": []}

    raw = result.stdout or ""
    page_match = PAGE_SIZE_RE.search(raw)
    try:
        page_width = float(page_match.group(1)) if page_match else 0
        page_height = float(page_match.group(2)) if page_match else 0
    except (TypeError, ValueError):
        page_width = 0
        page_height = 0

    words = []
    for match in WORD_BOX_RE.finditer(raw):
        word_text = html.unescape(re.sub(r"<[^>]+>", "", match.group(5))).strip()
        if not word_text:
            continue
        try:
            words.append({
                "text": word_text,
                "x0": float(match.group(1)),
                "y0": float(match.group(2)),
                "x1": float(match.group(3)),
                "y1": float(match.group(4)),
            })
        except (TypeError, ValueError):
            continue
    return {"width": page_width, "height": page_height, "words": words}


def score_page_for_keywords(page_no, page_count, text, keywords):
    lower = (text or "").lower()
    score = 0
    for keyword in keywords:
        if keyword in lower:
            score += 2
    if "figure" in lower or "fig." in lower or "fig " in lower or "图" in lower:
        score += 2
    if "table" in lower or "表" in lower:
        score += 2
    if page_no <= 2:
        score += 1
    if page_count and page_no >= max(page_count - 1, 1) and ("conclusion" in lower or "discussion" in lower):
        score += 1
    return score


def select_important_pages(page_texts, page_count, max_pages):
    if not page_count:
        page_count = len(page_texts)
    if page_count <= 0 or max_pages <= 0:
        return []

    role_rules = [
        ("方法总览图", ["figure", "fig.", "fig ", "framework", "pipeline", "architecture", "overview", "method", "图"]),
        ("核心方法页", ["method", "approach", "module", "algorithm", "framework", "pipeline"]),
        ("实验结果页", ["table", "result", "results", "experiment", "evaluation", "benchmark", "comparison", "metric", "dataset"]),
        ("分析或消融页", ["ablation", "analysis", "case study", "limitation", "discussion", "variant"]),
    ]
    page_payloads = []
    for page_index in range(page_count):
        text = page_texts[page_index] if page_index < len(page_texts) else ""
        page_payloads.append((page_index + 1, text, (text or "").lower()))

    used_pages = set()
    picked = []
    for label, keywords in role_rules:
        best = None
        for page_no, text, lower in page_payloads:
            if page_no in used_pages or not lower.strip():
                continue
            current_score = score_page_for_keywords(page_no, page_count, text, keywords)
            if current_score <= 0:
                continue
            if best is None or current_score > best[0]:
                best = (current_score, page_no, text)
        if best is not None:
            used_pages.add(best[1])
            picked.append({
                "page": best[1],
                "reason": label,
                "excerpt": clean_page_text(best[2], 900),
            })

    if len(picked) < max_pages:
        ranked = []
        fallback_keywords = [
            "figure", "fig.", "fig ", "table", "method", "approach", "experiment",
            "evaluation", "benchmark", "ablation", "analysis", "results", "dataset", "metric",
        ]
        for page_no, text, lower in page_payloads:
            if page_no in used_pages:
                continue
            current_score = score_page_for_keywords(page_no, page_count, text, fallback_keywords)
            if page_no == 1:
                current_score += 2
            ranked.append((current_score, page_no, text))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        for score, page_no, text in ranked:
            if page_no in used_pages:
                continue
            used_pages.add(page_no)
            picked.append({
                "page": page_no,
                "reason": "关键页面" if score > 0 else "补充页面",
                "excerpt": clean_page_text(text, 900),
            })
            if len(picked) >= max_pages:
                break

    if not picked:
        for page_no in sorted({1, min(2, page_count), min(4, page_count)}):
            if not page_no:
                continue
            text = page_texts[page_no - 1] if page_no - 1 < len(page_texts) else ""
            picked.append({
                "page": page_no,
                "reason": "默认候选页",
                "excerpt": clean_page_text(text, 900),
            })

    picked.sort(key=lambda item: item["page"])
    return picked[:max_pages]


def _crop_rendered_page_image(output_path, crop_rect):
    if Image is None or not crop_rect or not os.path.exists(output_path):
        return
    try:
        page_width = float(crop_rect.get("page_width") or 0)
        page_height = float(crop_rect.get("page_height") or 0)
        x0 = float(crop_rect.get("x0"))
        y0 = float(crop_rect.get("y0"))
        x1 = float(crop_rect.get("x1"))
        y1 = float(crop_rect.get("y1"))
    except (TypeError, ValueError):
        return
    if page_width <= 0 or page_height <= 0 or x1 <= x0 or y1 <= y0:
        return

    with Image.open(output_path) as image:
        scale_x = image.width / page_width
        scale_y = image.height / page_height
        left = max(0, min(image.width - 1, int(round(x0 * scale_x))))
        top = max(0, min(image.height - 1, int(round(y0 * scale_y))))
        right = max(left + 1, min(image.width, int(round(x1 * scale_x))))
        bottom = max(top + 1, min(image.height, int(round(y1 * scale_y))))
        if right - left < image.width * 0.15 or bottom - top < image.height * 0.08:
            return
        cropped = image.crop((left, top, right, bottom))
        cropped = _trim_white_margins(cropped)
        save_kwargs = {"quality": 92} if output_path.lower().endswith((".jpg", ".jpeg")) else {}
        cropped.save(output_path, **save_kwargs)


def _trim_white_margins(image):
    grayscale = image.convert("L")
    mask = grayscale.point(lambda pixel: 255 if pixel < 245 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return image
    left, top, right, bottom = bbox
    if right - left < image.width * 0.08 or bottom - top < image.height * 0.08:
        return image
    pad = max(4, int(max(image.width, image.height) * 0.015))
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(image.width, right + pad)
    bottom = min(image.height, bottom + pad)
    return image.crop((left, top, right, bottom))


def _pdftoppm_crop_args(crop_rect, image_width):
    if not crop_rect:
        return []
    try:
        page_width = float(crop_rect.get("page_width") or 0)
        page_height = float(crop_rect.get("page_height") or 0)
        x0 = float(crop_rect.get("x0"))
        y0 = float(crop_rect.get("y0"))
        x1 = float(crop_rect.get("x1"))
        y1 = float(crop_rect.get("y1"))
    except (TypeError, ValueError):
        return []
    if page_width <= 0 or page_height <= 0 or x1 <= x0 or y1 <= y0:
        return []
    scale = float(image_width) / max(page_width, page_height)
    left = max(0, int(round(x0 * scale)))
    top = max(0, int(round(y0 * scale)))
    width = max(1, int(round((x1 - x0) * scale)))
    height = max(1, int(round((y1 - y0) * scale)))
    return ["-x", str(left), "-y", str(top), "-W", str(width), "-H", str(height)]


def render_page_image(abs_pdf, file_key, page_no, workspace_root, quick_reading_cache_dir, image_width, base_name=None, crop_rect=None):
    cache_dir = paper_cache_dir(file_key, workspace_root, quick_reading_cache_dir)
    os.makedirs(cache_dir, exist_ok=True)

    safe_base_name = base_name or f"page_{page_no:03d}"
    target_jpg = os.path.join(cache_dir, safe_base_name + ".jpg")
    target_png = os.path.join(cache_dir, safe_base_name + ".png")
    prefix = os.path.join(cache_dir, safe_base_name)

    crop_args = _pdftoppm_crop_args(crop_rect, image_width)
    used_native_crop = bool(crop_args)
    cmd = [
        "pdftoppm",
        "-f", str(page_no),
        "-l", str(page_no),
        "-singlefile",
        "-scale-to", str(image_width),
        *crop_args,
        "-jpeg",
        abs_pdf,
        prefix,
    ]
    result = run_capture(cmd)
    if result is None or result.returncode != 0 or not os.path.exists(target_jpg):
        fallback = [
            "pdftoppm",
            "-f", str(page_no),
            "-l", str(page_no),
            "-singlefile",
            "-scale-to", str(image_width),
            *crop_args,
            "-png",
            abs_pdf,
            prefix,
        ]
        result = run_capture(fallback)
        if result is None or result.returncode != 0 or not os.path.exists(target_png):
            doc = open_pdf_doc(abs_pdf)
            try:
                if page_no < 1 or page_no > len(doc):
                    raise RuntimeError(f"第 {page_no} 页超出 PDF 页数范围")
                page = doc.load_page(page_no - 1)
                page_width = float(page.rect.width) or 1.0
                zoom = max(float(image_width) / page_width, 0.1)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                pixmap.save(target_png)
            finally:
                doc.close()
            if not os.path.exists(target_png):
                detail = ""
                if result is not None:
                    detail = clean_page_text(result.stderr or result.stdout, 300)
                raise RuntimeError(f"渲染第 {page_no} 页失败: {detail or '未知错误'}")
        if not used_native_crop:
            _crop_rendered_page_image(target_png, crop_rect)
        return os.path.relpath(target_png, workspace_root).replace(os.sep, "/")
    if not used_native_crop:
        _crop_rendered_page_image(target_jpg, crop_rect)
    return os.path.relpath(target_jpg, workspace_root).replace(os.sep, "/")


def build_grounding_text(page_texts, candidate_pages, max_source_chars):
    page_count = len(page_texts)
    chosen_pages = set()
    for page_no in [1, 2, 3, page_count - 1, page_count]:
        if 1 <= page_no <= page_count:
            chosen_pages.add(page_no)
    for item in candidate_pages:
        try:
            page_no = int(item.get("page") or 0)
        except (TypeError, ValueError):
            page_no = 0
        if 1 <= page_no <= page_count:
            chosen_pages.add(page_no)

    key_terms = [
        "abstract", "introduction", "method", "approach", "experiment", "evaluation",
        "baseline", "dataset", "metric", "ablation", "result", "conclusion", "limitation",
    ]
    for term in key_terms:
        for page_index, text in enumerate(page_texts, start=1):
            if term in (text or "").lower():
                chosen_pages.add(page_index)
                break

    blocks = []
    total_chars = 0
    for page_no in sorted(chosen_pages):
        text = page_texts[page_no - 1] if page_no - 1 < len(page_texts) else ""
        cleaned = clean_page_text(text, 4200)
        if not cleaned:
            continue
        block = f"=== Page {page_no} ===\n{cleaned}\n"
        if total_chars + len(block) > max_source_chars:
            break
        blocks.append(block)
        total_chars += len(block)
    return "\n".join(blocks)
