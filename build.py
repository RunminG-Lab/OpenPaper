import os
import json
import urllib.parse
import re
from datetime import datetime
from pathlib import Path

PDF_DIR = "papers"
METADATA_FILE = "metadata.json"
OUTPUT_HTML = "index.html"
TEMPLATE_HTML = "template.html"

os.makedirs(PDF_DIR, exist_ok=True)

# 根据文件名自动推断会议/年份（如果匹配不到则返回 None）
def infer_venue_and_year(fname):
    fname_lower = fname.lower()

    # ① Companion 匹配优先
    companion_patterns = [
        (r"(ase|icse|fse|issta|uist|chi|nips|aaai)(?:20)?(\d{2})comp", "{conf} Companion"),
    ]

    for pat, venue_template in companion_patterns:
        match = re.search(pat, fname_lower)
        if match:
            conf = match.group(1).upper()
            year_suffix = match.group(2)
            year = f"20{year_suffix}"
            venue = venue_template.format(conf=conf)
            return venue, year

    # ② 常规会议（覆盖 SE / AI / Security / Robotics / 图形 等顶会与顶刊）
    patterns = [
        # ---- 软件工程 (SE) ----
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

        # ---- AI / ML / NLP / Vision ----
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

        # ---- 安全 (Security) ----
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

        # ---- 机器人 / 控制 ----
        (r"icra(?:20)?(\d{2})", "ICRA"),
        (r"iros(?:20)?(\d{2})", "IROS"),
        (r"rss(?:20)?(\d{2})", "RSS"),
        (r"corl(?:20)?(\d{2})", "CoRL"),
        (r"humanoids(?:20)?(\d{2})", "Humanoids"),
        (r"ral(?:20)?(\d{2})", "RA-L"),
        (r"tro(?:20)?(\d{2})", "T-RO"),
        (r"ijrr(?:20)?(\d{2})", "IJRR"),

        # ---- 系统 / 体系结构 ----
        (r"osdi(?:20)?(\d{2})", "OSDI"),
        (r"sosp(?:20)?(\d{2})", "SOSP"),
        (r"atc(?:20)?(\d{2})", "USENIX ATC"),
        (r"eurosys(?:20)?(\d{2})", "EuroSys"),
        (r"asplos(?:20)?(\d{2})", "ASPLOS"),
        (r"isca(?:20)?(\d{2})", "ISCA"),
        (r"micro(?:20)?(\d{2})", "MICRO"),
        (r"hpca(?:20)?(\d{2})", "HPCA"),

        # ---- HCI / 图形 / 其它 ----
        (r"ismar(?:20)?(\d{2})", "ISMAR"),
        (r"uist(?:20)?(\d{2})", "UIST"),
        (r"iva(?:20)?(\d{2})", "IVA"),
        (r"chi(?:20)?(\d{2})", "CHI"),
        (r"siggraph(?:20)?(\d{2})", "SIGGRAPH"),
        (r"siggraphasia(?:20)?(\d{2})", "SIGGRAPH Asia"),
        (r"tog(?:20)?(\d{2})", "TOG"),
    ]

    for pat, venue_name in patterns:
        match = re.search(pat, fname_lower)
        if match:
            year_suffix = match.group(1)
            year = f"20{year_suffix}"
            return venue_name, year

    return None, None

    fname_lower = fname.lower()
    patterns = [
        (r"ase(?:20)?(\d{2})", "ASE"),
        (r"icse(?:20)?(\d{2})", "ICSE"),
        (r"fse(?:20)?(\d{2})", "FSE"),
        (r"sbst(?:20)?(\d{2})", "SBST"),
        (r"issta(?:20)?(\d{2})", "ISSTA"),
        (r"acl(?:20)?(\d{2})", "ACL"),
        (r"arxiv(?:20)?(\d{2})", "arXiv"),
        (r"nip(?:s)?(?:20)?(\d{2})", "NeurIPS"),
        (r"cvpr(?:20)?(\d{2})", "CVPR"),
        (r"ismar(?:20)?(\d{2})", "ISMAR"),
        (r"iccv(?:20)?(\d{2})", "ICCV"),
        (r"eccv(?:20)?(\d{2})", "ECCV"),
        (r"aaai(?:20)?(\d{2})", "AAAI"),
        (r"icra(?:20)?(\d{2})", "ICRA"),
        (r"uist(?:20)?(\d{2})", "UIST"),
        (r"sec(?:20)?(\d{2})", "Usenix SEC"),
        (r"iva(?:20)?(\d{2})", "IVA"),
        (r"chi(?:20)?(\d{2})", "CHI"),
        (r"siggraph(?:20)?(\d{2})", "SIGGRAPH"),
    ]
    for pat, venue_name in patterns:
        match = re.search(pat, fname_lower)
        if match:
            year_suffix = match.group(1)
            year = f"20{year_suffix}"
            return venue_name, year
    return None, None

# 加载或创建 metadata
if os.path.exists(METADATA_FILE):
    with open(METADATA_FILE, "r", encoding="utf-8") as f:
        metadata = json.load(f)
else:
    metadata = {}

# 扫描 PDF 文件
papers = []
pdf_files = []
for root, _, files in os.walk(PDF_DIR):
    for fname in files:
        if fname.lower().endswith(".pdf"):
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, PDF_DIR)
            rel_path = rel_path.replace(os.sep, "/")
            pdf_files.append((rel_path, fname))

pdf_files.sort(key=lambda item: item[0].lower())
legacy_keys_to_remove = set()

for rel_path, fname in pdf_files:
    key = rel_path.lower()
    legacy_key = fname.lower()

    info = metadata.get(key) or metadata.get(legacy_key, {})
    if legacy_key in metadata and key != legacy_key:
        legacy_keys_to_remove.add(legacy_key)

    # URL 编码路径
    quoted_rel_path = "/".join(urllib.parse.quote(part) for part in rel_path.split("/"))

    # 自动生成 tag：如果 metadata 中没有 tags 或为空，则使用 PDF 所在的一级文件夹
    folder_tag = rel_path.split("/")[0] if "/" in rel_path else "unsorted"
    tags = info.get("tags")
    if not tags:
        tags = [folder_tag]

    # 自动从文件名推断年份和会议
    year = info.get("year")
    venue = info.get("venue")
    if not year or not venue:
        inferred_venue, inferred_year = infer_venue_and_year(fname)
        if not year:
            year = inferred_year  # 只有匹配到才填
        if not venue:
            venue = inferred_venue  # 只有匹配到才填

    # 加入时间：优先使用 metadata 中已记录的 added_at，否则取文件 ctime/mtime 的较大者
    # （Windows 下 st_ctime 是创建时间；mtime 兜底可以反映后续移动/编辑的时间）
    added_at = info.get("added_at")
    if not added_at:
        try:
            stat = os.stat(os.path.join(PDF_DIR, rel_path))
            ctime = getattr(stat, "st_birthtime", None) or stat.st_ctime
            mtime = stat.st_mtime
            ts = max(ctime, mtime)
            added_at = datetime.fromtimestamp(ts).isoformat(timespec="seconds")
        except OSError:
            added_at = ""

    paper = {
        "file_key": key,
        "title": info.get("title", os.path.splitext(fname)[0]),
        "authors": info.get("authors", "Unknown"),
        "year": year if year else "",      
        "venue": venue if venue else "",   
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

# 删除 legacy key
for legacy_key in legacy_keys_to_remove:
    metadata.pop(legacy_key, None)

# ✅ 清理 metadata 中已不存在的 PDF
existing_keys = set([rel_path.lower() for rel_path, _ in pdf_files])
keys_to_remove = [k for k in metadata if k not in existing_keys]
for k in keys_to_remove:
    print(f"🗑️ 删除已不存在的 PDF 对应 metadata: {k}")
    metadata.pop(k, None)

# 保存 metadata.json
with open(METADATA_FILE, "w", encoding="utf-8") as f:
    json.dump(metadata, f, indent=2, ensure_ascii=False)

# 渲染 HTML
if not os.path.exists(TEMPLATE_HTML):
    raise FileNotFoundError(f"{TEMPLATE_HTML} 不存在")

with open(TEMPLATE_HTML, "r", encoding="utf-8") as f:
    html = f.read()

html = html.replace("const EMBEDDED_PAPERS = [];", f"const EMBEDDED_PAPERS = {json.dumps(papers, ensure_ascii=False)};")

with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
    f.write(html)

print(f"✅ 成功生成 {OUTPUT_HTML}，共 {len(papers)} 篇论文")
