import json
import os
import sys
import urllib.parse

PDF_DIR = "papers"
METADATA_FILE = "metadata.json"


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> None:
    configure_stdio()

    if not os.path.exists(METADATA_FILE):
        print("metadata.json not found, nothing to repair")
        return

    with open(METADATA_FILE, "r", encoding="utf-8") as file_obj:
        old_metadata = json.load(file_obj)

    filename_to_meta = {}
    for key, info in old_metadata.items():
        filename = os.path.basename(key).lower()
        filename_to_meta.setdefault(filename, []).append((key, info))

    new_metadata = {}
    repaired = 0
    matched_old_keys = set()

    for root, _, files in os.walk(PDF_DIR):
        for filename in files:
            if not filename.lower().endswith(".pdf"):
                continue

            abs_path = os.path.join(root, filename)
            rel_path = os.path.relpath(abs_path, PDF_DIR).replace("\\", "/")
            filename_lower = filename.lower()

            candidates = filename_to_meta.get(filename_lower)
            if not candidates:
                print(f"skip new pdf without previous metadata: {rel_path}")
                continue

            old_key, info = candidates[0]
            matched_old_keys.add(old_key)

            quoted_rel_path = "/".join(urllib.parse.quote(part) for part in rel_path.split("/"))
            file_key = rel_path.lower()
            new_metadata[file_key] = {
                **info,
                "file_key": file_key,
                "pdf": f"{PDF_DIR}/{quoted_rel_path}",
                "pdf_local": f"{PDF_DIR}/{quoted_rel_path}",
            }
            repaired += 1

    lost_keys = [key for key in old_metadata.keys() if key not in matched_old_keys]
    for key in lost_keys:
        print(f"unmatched metadata entry: {key}")

    with open(METADATA_FILE, "w", encoding="utf-8") as file_obj:
        json.dump(new_metadata, file_obj, indent=2, ensure_ascii=False)

    print("======================")
    print(f"repaired entries: {repaired}")
    print(f"unmatched entries: {len(lost_keys)}")
    print("======================")


if __name__ == "__main__":
    main()
