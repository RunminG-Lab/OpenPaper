import json
import os
import re
import sys
import urllib.parse
from datetime import datetime

PDF_DIR = "papers"
METADATA_FILE = "metadata.json"
OUTPUT_HTML = "index.html"
TEMPLATE_HTML = "template.html"

COMPANION_PATTERNS = [
    (r"(ase|icse|fse|issta|uist|chi|nips|aaai)(?:20)?(\d{2})comp", "{conf} Companion"),
]

VENUE_PATTERNS = [
    (r"ase(?:20)?(\d{2})", "ASE"),
    (r"icse(?:20)?(\d{2})", "ICSE"),
    (r"fse(?:20)?(\d{2})", "FSE"),
    (r"esecfse(?:20)?(\d{2})", "ESEC/FSE"),
    (r"sbst(?:20)?(\d{2})", "SBST"),
    (r"issta(?:20)?(\d{2})", "ISSTA"),
    (r"icsme(?:20)?(\d{2})", "ICSME"),
    (r"msr(?:20)?(\d{2})", "MSR"),
    (r"saner(?:20)?(\d{2})", "SANER"),
    (r"icpc(?:20)?(\d{2})", "ICPC"),
    (r"models(?:20)?(\d{2})", "MODELS"),
    (r"oopsla(?:20)?(\d{2})", "OOPSLA"),
    (r"pldi(?:20)?(\d{2})", "PLDI"),
    (r"popl(?:20)?(\d{2})", "POPL"),
    (r"ecoop(?:20)?(\d{2})", "ECOOP"),
    (r"tosem(?:20)?(\d{2})", "TOSEM"),
    (r"tse(?:20)?(\d{2})", "TSE"),
    (r"jss(?:20)?(\d{2})", "JSS"),
    (r"emse(?:20)?(\d{2})", "EMSE"),
    (r"acl(?:20)?(\d{2})", "ACL"),
    (r"emnlp(?:20)?(\d{2})", "EMNLP"),
    (r"naacl(?:20)?(\d{2})", "NAACL"),
    (r"coling(?:20)?(\d{2})", "COLING"),
    (r"arxiv(?:20)?(\d{2})", "arXiv"),
    (r"nip(?:s)?(?:20)?(\d{2})", "NeurIPS"),
    (r"neurips(?:20)?(\d{2})", "NeurIPS"),
    (r"icml(?:20)?(\d{2})", "ICML"),
    (r"iclr(?:20)?(\d{2})", "ICLR"),
    (r"aaai(?:20)?(\d{2})", "AAAI"),
    (r"ijcai(?:20)?(\d{2})", "IJCAI"),
    (r"kdd(?:20)?(\d{2})", "KDD"),
    (r"www(?:20)?(\d{2})", "WWW"),
    (r"cvpr(?:20)?(\d{2})", "CVPR"),
    (r"iccv(?:20)?(\d{2})", "ICCV"),
    (r"eccv(?:20)?(\d{2})", "ECCV"),
    (r"mm(?:20)?(\d{2})", "ACM MM"),
    (r"acmmm(?:20)?(\d{2})", "ACM MM"),
    (r"tpami(?:20)?(\d{2})", "TPAMI"),
    (r"jmlr(?:20)?(\d{2})", "JMLR"),
    (r"tacl(?:20)?(\d{2})", "TACL"),
    (r"colm(?:20)?(\d{2})", "COLM"),
    (r"sec(?:20)?(\d{2})", "USENIX Security"),
    (r"usenix(?:sec)?(?:20)?(\d{2})", "USENIX Security"),
    (r"sp(?:20)?(\d{2})", "IEEE S&P"),
    (r"oakland(?:20)?(\d{2})", "IEEE S&P"),
    (r"ccs(?:20)?(\d{2})", "ACM CCS"),
    (r"ndss(?:20)?(\d{2})", "NDSS"),
    (r"eurosp(?:20)?(\d{2})", "EuroS&P"),
    (r"asiaccs(?:20)?(\d{2})", "AsiaCCS"),
    (r"raid(?:20)?(\d{2})", "RAID"),
    (r"dsn(?:20)?(\d{2})", "DSN"),
    (r"acsac(?:20)?(\d{2})", "ACSAC"),
    (r"tifs(?:20)?(\d{2})", "TIFS"),
    (r"tdsc(?:20)?(\d{2})", "TDSC"),
    (r"icra(?:20)?(\d{2})", "ICRA"),
    (r"iros(?:20)?(\d{2})", "IROS"),
    (r"rss(?:20)?(\d{2})", "RSS"),
    (r"corl(?:20)?(\d{2})", "CoRL"),
    (r"humanoids(?:20)?(\d{2})", "Humanoids"),
    (r"ral(?:20)?(\d{2})", "RA-L"),
    (r"tro(?:20)?(\d{2})", "T-RO"),
    (r"ijrr(?:20)?(\d{2})", "IJRR"),
    (r"osdi(?:20)?(\d{2})", "OSDI"),
    (r"sosp(?:20)?(\d{2})", "SOSP"),
    (r"atc(?:20)?(\d{2})", "USENIX ATC"),
    (r"eurosys(?:20)?(\d{2})", "EuroSys"),
    (r"asplos(?:20)?(\d{2})", "ASPLOS"),
    (r"isca(?:20)?(\d{2})", "ISCA"),
    (r"micro(?:20)?(\d{2})", "MICRO"),
    (r"hpca(?:20)?(\d{2})", "HPCA"),
    (r"ismar(?:20)?(\d{2})", "ISMAR"),
    (r"uist(?:20)?(\d{2})", "UIST"),
    (r"iva(?:20)?(\d{2})", "IVA"),
    (r"chi(?:20)?(\d{2})", "CHI"),
    (r"siggraph(?:20)?(\d{2})", "SIGGRAPH"),
    (r"siggraphasia(?:20)?(\d{2})", "SIGGRAPH Asia"),
    (r"tog(?:20)?(\d{2})", "TOG"),
]


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def infer_venue_and_year(filename: str):
    filename_lower = filename.lower()

    for pattern, venue_template in COMPANION_PATTERNS:
        match = re.search(pattern, filename_lower)
        if not match:
            continue
        conf = match.group(1).upper()
        year = f"20{match.group(2)}"
        return venue_template.format(conf=conf), year

    for pattern, venue_name in VENUE_PATTERNS:
        match = re.search(pattern, filename_lower)
        if not match:
            continue
        year = f"20{match.group(1)}"
        return venue_name, year

    return None, None


def main() -> None:
    configure_stdio()
    os.makedirs(PDF_DIR, exist_ok=True)

    if os.path.exists(METADATA_FILE):
        with open(METADATA_FILE, "r", encoding="utf-8") as file_obj:
            metadata = json.load(file_obj)
    else:
        metadata = {}

    papers = []
    pdf_files = []
    for root, _, files in os.walk(PDF_DIR):
        for filename in files:
            if filename.lower().endswith(".pdf"):
                abs_path = os.path.join(root, filename)
                rel_path = os.path.relpath(abs_path, PDF_DIR).replace(os.sep, "/")
                pdf_files.append((rel_path, filename))

    pdf_files.sort(key=lambda item: item[0].lower())
    legacy_keys_to_remove = set()

    for rel_path, filename in pdf_files:
        key = rel_path.lower()
        legacy_key = filename.lower()

        info = metadata.get(key) or metadata.get(legacy_key, {})
        if legacy_key in metadata and key != legacy_key:
            legacy_keys_to_remove.add(legacy_key)

        quoted_rel_path = "/".join(urllib.parse.quote(part) for part in rel_path.split("/"))
        folder_tag = rel_path.split("/")[0] if "/" in rel_path else "unsorted"
        tags = info.get("tags") or [folder_tag]

        year = info.get("year")
        venue = info.get("venue")
        if not year or not venue:
            inferred_venue, inferred_year = infer_venue_and_year(filename)
            if not year:
                year = inferred_year
            if not venue:
                venue = inferred_venue

        added_at = info.get("added_at")
        if not added_at:
            try:
                stat = os.stat(os.path.join(PDF_DIR, rel_path))
                ctime = getattr(stat, "st_birthtime", None) or stat.st_ctime
                mtime = stat.st_mtime
                added_at = datetime.fromtimestamp(max(ctime, mtime)).isoformat(timespec="seconds")
            except OSError:
                added_at = ""

        paper = {
            "file_key": key,
            "title": info.get("title", os.path.splitext(filename)[0]),
            "authors": info.get("authors", "Unknown"),
            "year": year or "",
            "venue": venue or "",
            "tags": tags,
            "pdf": f"{PDF_DIR}/{quoted_rel_path}",
            "pdf_local": f"{PDF_DIR}/{quoted_rel_path}",
            "read": info.get("read", False),
            "bib": info.get("bib", ""),
            "notes": info.get("notes", ""),
            "speed_read": info.get("speed_read"),
            "added_at": added_at,
        }

        papers.append(paper)
        metadata[key] = paper

    for legacy_key in legacy_keys_to_remove:
        metadata.pop(legacy_key, None)

    existing_keys = {rel_path.lower() for rel_path, _ in pdf_files}
    keys_to_remove = [key for key in metadata if key not in existing_keys]
    for key in keys_to_remove:
        print(f"remove stale metadata for missing pdf: {key}")
        metadata.pop(key, None)

    with open(METADATA_FILE, "w", encoding="utf-8") as file_obj:
        json.dump(metadata, file_obj, indent=2, ensure_ascii=False)

    if not os.path.exists(TEMPLATE_HTML):
        raise FileNotFoundError(f"{TEMPLATE_HTML} not found")

    with open(TEMPLATE_HTML, "r", encoding="utf-8") as file_obj:
        html = file_obj.read()

    html = html.replace("const EMBEDDED_PAPERS = [];", f"const EMBEDDED_PAPERS = {json.dumps(papers, ensure_ascii=False)};")

    with open(OUTPUT_HTML, "w", encoding="utf-8") as file_obj:
        file_obj.write(html)

    print(f"generated {OUTPUT_HTML} with {len(papers)} papers")


if __name__ == "__main__":
    main()
