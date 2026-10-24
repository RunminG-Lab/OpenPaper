"""Regression checks for tag deletion; all files live in temporary workspaces."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build
from backend import metadata


class TagRecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openpaper-tags-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.papers = self.root / "papers"
        self.papers.mkdir()
        self.meta_file = self.papers / "metadata.json"
        self.recycle = self.root / ".recycle_bin"
        self.cache = self.papers / ".quick_reading_cache"
        self.entries = {
            "other/a.pdf": self.paper("other/a.pdf", ["Agent", "Topic", "LLM"]),
            "other/b.pdf": self.paper("other/b.pdf", ["Topic"]),
            "other/c.pdf": self.paper("other/c.pdf", ["VR"]),
        }
        for key in self.entries:
            path = self.papers / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.4 test fixture")
        self.write_metadata(self.entries)

    @staticmethod
    def paper(key, tags):
        return {
            "file_key": key,
            "title": key,
            "authors": "A. Author",
            "tags": tags,
            "pdf": "papers/" + key,
            "pdf_local": "papers/" + key,
            "notes": "original notes",
            "read": False,
        }

    def write_metadata(self, value):
        self.meta_file.write_text(json.dumps(value), encoding="utf-8")

    def read_metadata(self):
        return json.loads(self.meta_file.read_text(encoding="utf-8"))

    def delete_tag(self, tag="Topic"):
        return metadata.delete_tag(tag, str(self.root), str(self.meta_file), str(self.recycle))

    def restore(self, rid):
        with patch("backend.metadata.subprocess.run"):
            return metadata.restore_recycle_item(
                rid, str(self.root), str(self.root / "build.py"),
                str(self.meta_file), str(self.recycle), str(self.cache),
            )

    def assert_ok(self, result):
        self.assertEqual(result.status, 200, result.payload)
        self.assertTrue(result.payload["ok"])

    def test_delete_removes_all_associations_and_restore_recovers_exact_tags(self):
        original = copy.deepcopy(self.entries)
        result = self.delete_tag()
        self.assert_ok(result)
        current = self.read_metadata()
        self.assertEqual(current["other/a.pdf"]["tags"], ["Agent", "LLM"])
        self.assertEqual(current["other/b.pdf"]["tags"], [])
        self.assertEqual(current["other/c.pdf"], original["other/c.pdf"])
        listed = metadata.list_recycle_bin(str(self.recycle)).payload["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["item_type"], "tag")
        self.assert_ok(self.restore(result.payload["id"]))
        self.assertEqual(self.read_metadata(), original)
        self.assertEqual(metadata.list_recycle_bin(str(self.recycle)).payload["items"], [])

    def test_nonempty_corresponding_folder_blocks_without_any_changes(self):
        folder = self.papers / "Topic"
        folder.mkdir()
        (folder / "notes.txt").write_text("Move me first", encoding="utf-8")
        before = self.meta_file.read_bytes()
        result = self.delete_tag()
        self.assertEqual(result.status, 409, result.payload)
        self.assertRegex(result.payload["error"], "搬迁|迁移|移走|移动")
        self.assertEqual(self.meta_file.read_bytes(), before)
        self.assertTrue((folder / "notes.txt").exists())
        self.assertEqual(metadata.list_recycle_bin(str(self.recycle)).payload["items"], [])

    def test_subdirectory_also_counts_as_folder_content(self):
        (self.papers / "Topic" / "nested").mkdir(parents=True)
        result = self.delete_tag()
        self.assertEqual(result.status, 409, result.payload)
        self.assertEqual(self.read_metadata(), self.entries)

    def test_empty_corresponding_folder_is_removed_and_restored(self):
        folder = self.papers / "Topic"
        folder.mkdir()
        result = self.delete_tag()
        self.assert_ok(result)
        self.assertFalse(folder.exists())
        self.assert_ok(self.restore(result.payload["id"]))
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.read_metadata(), self.entries)

    def test_restore_keeps_later_unrelated_edits(self):
        result = self.delete_tag()
        self.assert_ok(result)
        current = self.read_metadata()
        current["other/a.pdf"]["notes"] = "new notes"
        current["other/a.pdf"]["read"] = True
        current["other/a.pdf"]["tags"].append("New tag")
        self.write_metadata(current)
        self.assert_ok(self.restore(result.payload["id"]))
        restored = self.read_metadata()["other/a.pdf"]
        self.assertEqual(restored["notes"], "new notes")
        self.assertTrue(restored["read"])
        self.assertEqual(restored["tags"], ["Agent", "Topic", "LLM", "New tag"])

    def test_multiple_deleted_tags_restore_independently_in_either_order(self):
        for deletion_order in (("A", "B"), ("B", "A")):
            for restore_order in (("A", "B"), ("B", "A")):
                with self.subTest(delete=deletion_order, restore=restore_order):
                    current = copy.deepcopy(self.entries)
                    current["other/a.pdf"]["tags"] = ["A", "B", "C"]
                    self.write_metadata(current)
                    deleted = {}
                    for tag in deletion_order:
                        result = self.delete_tag(tag)
                        self.assert_ok(result)
                        deleted[tag] = result.payload["id"]
                    self.assertEqual(self.read_metadata()["other/a.pdf"]["tags"], ["C"])
                    self.assert_ok(self.restore(deleted[restore_order[0]]))
                    intermediate = self.read_metadata()["other/a.pdf"]["tags"]
                    self.assertIn(restore_order[0], intermediate)
                    self.assertNotIn(restore_order[1], intermediate)
                    self.assert_ok(self.restore(deleted[restore_order[1]]))
                    self.assertEqual(self.read_metadata()["other/a.pdf"]["tags"], ["A", "B", "C"])

    def test_rebuild_keeps_explicit_empty_tags_and_defaults_only_new_papers(self):
        result = self.delete_tag()
        self.assert_ok(result)
        new_pdf = self.papers / "Fresh" / "new.pdf"
        new_pdf.parent.mkdir()
        new_pdf.write_bytes(b"%PDF-1.4 new")
        template = self.root / "template.html"
        template.write_text("<html>fixture</html>", encoding="utf-8")
        with patch.multiple(
            build, PDF_DIR=str(self.papers), METADATA_FILE=str(self.meta_file),
            QUICK_READING_CACHE_DIR=str(self.cache), TEMPLATE_HTML=str(template),
            OUTPUT_HTML=str(self.root / "index.html"),
        ), patch.object(build, "rebuild_speedread_index"), patch.object(build, "configure_stdio"):
            build.main()
        rebuilt = self.read_metadata()
        self.assertEqual(rebuilt["other/b.pdf"]["tags"], [])
        self.assertEqual(rebuilt["fresh/new.pdf"]["tags"], ["Fresh"])
        self.assertTrue(all("Topic" not in p["tags"] for p in rebuilt.values()))

    def test_unknown_tag_does_not_create_recycle_item(self):
        before = self.meta_file.read_bytes()
        result = self.delete_tag("Missing")
        self.assertEqual(result.status, 404, result.payload)
        self.assertEqual(self.meta_file.read_bytes(), before)
        self.assertEqual(metadata.list_recycle_bin(str(self.recycle)).payload["items"], [])

    def test_purging_tag_snapshot_does_not_restore_associations(self):
        result = self.delete_tag()
        self.assert_ok(result)
        self.assert_ok(metadata.purge_paper(result.payload["id"], str(self.recycle)))
        self.assertEqual(metadata.list_recycle_bin(str(self.recycle)).payload["items"], [])
        self.assertTrue(all("Topic" not in p["tags"] for p in self.read_metadata().values()))

    def test_failed_delete_rolls_back_metadata_recycled_paper_and_empty_folder(self):
        folder = self.papers / "Topic"
        folder.mkdir()
        key = "other/a.pdf"
        paper_result = metadata.delete_paper(
            key, str(self.root), str(self.meta_file), str(self.recycle),
        )
        self.assert_ok(paper_result)
        current = self.read_metadata()
        current.pop(key)
        self.write_metadata(current)
        info_file = self.recycle / paper_result.payload["id"] / "info.json"
        original_info = json.loads(info_file.read_text(encoding="utf-8"))
        original_write = metadata.atomic_write_metadata
        calls = []

        def fail_after_first_write(data, target):
            original_write(data, target)
            calls.append(target)
            if len(calls) == 1:
                raise OSError("simulated stats write failure")

        with patch.object(metadata, "atomic_write_metadata", side_effect=fail_after_first_write):
            result = self.delete_tag()
        self.assertEqual(result.status, 500, result.payload)
        self.assertEqual(self.read_metadata(), current)
        self.assertEqual(json.loads(info_file.read_text(encoding="utf-8")), original_info)
        self.assertTrue(folder.is_dir())
        self.assertEqual(len(metadata.list_recycle_bin(str(self.recycle)).payload["items"]), 1)

    def test_failed_restore_keeps_snapshot_and_can_be_retried(self):
        folder = self.papers / "Topic"
        folder.mkdir()
        deleted = self.delete_tag()
        self.assert_ok(deleted)
        current = self.read_metadata()
        original_write = metadata.atomic_write_metadata
        calls = []

        def fail_after_first_write(data, target):
            original_write(data, target)
            calls.append(target)
            if len(calls) == 1:
                raise OSError("simulated stats write failure")

        with patch.object(metadata, "atomic_write_metadata", side_effect=fail_after_first_write):
            result = self.restore(deleted.payload["id"])
        self.assertEqual(result.status, 500, result.payload)
        self.assertEqual(self.read_metadata(), current)
        self.assertFalse(folder.exists())
        self.assertTrue((self.recycle / deleted.payload["id"] / "info.json").exists())
        self.assert_ok(self.restore(deleted.payload["id"]))
        self.assertEqual(self.read_metadata(), self.entries)
        self.assertTrue(folder.is_dir())

    def test_existing_paper_delete_and_restore_preserves_metadata_and_pdf(self):
        key = "other/a.pdf"
        pdf = self.papers / key
        content = pdf.read_bytes()
        result = metadata.delete_paper(key, str(self.root), str(self.meta_file), str(self.recycle))
        self.assert_ok(result)
        self.assertFalse(pdf.exists())
        current = self.read_metadata()
        current.pop(key)
        self.write_metadata(current)
        self.assert_ok(self.restore(result.payload["id"]))
        self.assertEqual(pdf.read_bytes(), content)
        self.assertEqual(self.read_metadata()[key], self.entries[key])

    def test_existing_speedread_delete_and_restore_preserves_cache(self):
        key = "other/a.pdf"
        cache_dir = self.cache / hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        cache_dir.mkdir(parents=True)
        content = '{"summary": "keep me"}'
        (cache_dir / "speed_read.json").write_text(content, encoding="utf-8")
        (self.cache / "index.json").write_text(
            json.dumps({"items": {key: {"status": "ready"}}}), encoding="utf-8",
        )
        result = metadata.delete_speedread(
            key, str(self.root), str(self.meta_file), str(self.cache), str(self.recycle),
        )
        self.assert_ok(result)
        self.assertFalse(cache_dir.exists())
        with patch("backend.quick_reading.rebuild_speedread_index"):
            self.assert_ok(self.restore(result.payload["id"]))
        self.assertEqual((cache_dir / "speed_read.json").read_text(encoding="utf-8"), content)
        self.assertEqual(self.read_metadata(), self.entries)

    def test_recycled_paper_never_resurrects_deleted_tag_and_undo_recovers_it(self):
        for tag_first in (True, False):
            with self.subTest(restore_tag_first=tag_first):
                key = "other/a.pdf"
                paper_result = metadata.delete_paper(
                    key, str(self.root), str(self.meta_file), str(self.recycle),
                )
                self.assert_ok(paper_result)
                current = self.read_metadata()
                current.pop(key)
                self.write_metadata(current)
                tag_result = self.delete_tag()
                self.assert_ok(tag_result)
                if tag_first:
                    self.assert_ok(self.restore(tag_result.payload["id"]))
                    self.assert_ok(self.restore(paper_result.payload["id"]))
                else:
                    self.assert_ok(self.restore(paper_result.payload["id"]))
                    self.assertEqual(self.read_metadata()[key]["tags"], ["Agent", "LLM"])
                    self.assert_ok(self.restore(tag_result.payload["id"]))
                self.assertEqual(self.read_metadata(), self.entries)

    def test_clear_recycle_bin_handles_mixed_paper_and_tag_items(self):
        self.assert_ok(self.delete_tag())
        self.assert_ok(metadata.delete_paper(
            "other/c.pdf", str(self.root), str(self.meta_file), str(self.recycle),
        ))
        result = metadata.purge_all_papers(str(self.recycle))
        self.assert_ok(result)
        self.assertEqual(result.payload["count"], 2)
        self.assertEqual(metadata.list_recycle_bin(str(self.recycle)).payload["items"], [])


if __name__ == "__main__":
    unittest.main()
