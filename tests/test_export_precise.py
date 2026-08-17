import json
import os
import tempfile
import unittest

from export_precise import (
    ExportError,
    backup_existing_export,
    get_last_chapter_title,
    merge_block_sequences,
    next_catalog_index,
    next_catalog_title,
    partition_catalog_sections,
    resolve_catalog_title,
    save_chapter,
    select_catalog_section_blocks,
    validate_export,
)


class CatalogTitleTests(unittest.TestCase):
    def setUp(self):
        self.titles = ["版权信息", "第一章 开始", "尾声"]

    def test_resolves_live_title_and_progress_suffix(self):
        self.assertEqual(resolve_catalog_title(" 第一章  开始 ", self.titles), "第一章 开始")
        self.assertEqual(resolve_catalog_title("尾声当前读到 99%", self.titles), "尾声")
        self.assertEqual(resolve_catalog_title("未知章节", self.titles), "")

    def test_selects_first_or_next_catalog_title(self):
        self.assertEqual(next_catalog_title(self.titles), "版权信息")
        self.assertEqual(next_catalog_title(self.titles, "版权信息"), "第一章 开始")
        self.assertIsNone(next_catalog_title(self.titles, "尾声"))
        with self.assertRaises(ExportError):
            next_catalog_title(self.titles, "不存在")

    def test_duplicate_title_resume_uses_catalog_index(self):
        titles = ["第一部分", "｜投资随想录｜", "第二部分", "｜投资随想录｜", "第三部分"]

        self.assertEqual(
            next_catalog_index(
                titles, resume_after_index=3,
                resume_after_title="｜投资随想录｜"),
            4)
        with self.assertRaises(ExportError):
            next_catalog_title(titles, "｜投资随想录｜")
        with self.assertRaises(ExportError):
            next_catalog_index(
                titles, resume_after_index=3,
                resume_after_title="第二部分")
        with self.assertRaises(ExportError):
            next_catalog_index(
                titles, resume_after_index=417,
                resume_after_title="｜投资随想录｜")

    def test_duplicate_sections_are_partitioned_by_catalog_index(self):
        titles = ["第一部分", "｜投资随想录｜", "第二部分", "｜投资随想录｜", "第三部分"]
        blocks = [
            {"type": "text", "text": "｜投资随想录｜"},
            {"type": "text", "text": "第一篇随想"},
            {"type": "text", "text": "第二部分"},
            {"type": "text", "text": "第二部分正文"},
            {"type": "text", "text": "｜投资随想录｜"},
            {"type": "text", "text": "第二篇随想"},
            {"type": "text", "text": "第三部分"},
        ]

        sections, complete = partition_catalog_sections(blocks, 1, titles)

        self.assertEqual(
            [block["text"] for block in sections[1]], ["第一篇随想"])
        self.assertEqual(
            [block["text"] for block in sections[3]], ["第二篇随想"])
        self.assertTrue(complete[1])
        self.assertTrue(complete[3])

        second, found_next = select_catalog_section_blocks(
            blocks[4:], titles[3], titles, current_index=3)
        self.assertEqual(
            [block["text"] for block in second], ["第二篇随想"])
        self.assertTrue(found_next)

    def test_rendered_chunk_is_cut_at_catalog_section_boundaries(self):
        titles = ["第十四章 路线", "第1周：看清自己", "第2周：说清你要什么"]
        blocks = [
            {"type": "text", "text": "第十四章"},
            {"type": "text", "text": "路线正文"},
            {"type": "text", "text": "第1周：看清自己"},
            {"type": "text", "text": "第一周正文"},
            {"type": "text", "text": "第2周：说清你要什么"},
            {"type": "text", "text": "第二周正文"},
        ]

        chapter, found_next = select_catalog_section_blocks(blocks, titles[0], titles)
        week_one, week_one_found_next = select_catalog_section_blocks(
            blocks, titles[1], titles)

        self.assertEqual([block["text"] for block in chapter], ["第十四章", "路线正文"])
        self.assertTrue(found_next)
        self.assertEqual([block["text"] for block in week_one], ["第一周正文"])
        self.assertTrue(week_one_found_next)

    def test_virtualized_windows_merge_by_largest_overlap(self):
        first = [{"type": "text", "text": value} for value in ["A", "B", "C"]]
        second = [{"type": "text", "text": value} for value in ["B", "C", "D"]]
        merged = merge_block_sequences(first, second)
        self.assertEqual([block["text"] for block in merged], ["A", "B", "C", "D"])


class ExportFileTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = self.tempdir.name
        self.md_dir = os.path.join(self.root, "chapters")
        self.raw_dir = os.path.join(self.root, "raw")
        os.makedirs(self.md_dir)
        os.makedirs(self.raw_dir)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_empty_title_is_rejected(self):
        with self.assertRaises(ExportError):
            save_chapter("", [], 1, self.md_dir, self.raw_dir)

    def test_empty_but_named_chapter_is_saved_and_checkpoint_is_readable(self):
        save_chapter("版权信息", [], 1, self.md_dir, self.raw_dir)

        title, index = get_last_chapter_title(self.md_dir)
        self.assertEqual((title, index), ("版权信息", 1))
        with open(os.path.join(self.raw_dir, "0001.json")) as raw_file:
            self.assertEqual(json.load(raw_file)["title"], "版权信息")

    def test_validation_requires_exact_catalog_order_and_matching_files(self):
        titles = ["版权信息", "第一章 开始"]
        save_chapter(
            "版权信息", [{"type": "text", "text": "版权正文"}],
            1, self.md_dir, self.raw_dir)
        save_chapter(
            "第一章 开始", [{"type": "text", "text": "第一章正文"}],
            2, self.md_dir, self.raw_dir)
        self.assertEqual(validate_export(titles, self.md_dir, self.raw_dir), [])

        os.remove(os.path.join(self.raw_dir, "0002.json"))
        errors = validate_export(titles, self.md_dir, self.raw_dir)
        self.assertTrue(any("文件编号不一致" in error for error in errors))
        self.assertTrue(any("目录覆盖不完整" in error for error in errors))

    def test_validation_rejects_empty_named_chapter(self):
        save_chapter("版权信息", [], 1, self.md_dir, self.raw_dir)
        errors = validate_export(["版权信息"], self.md_dir, self.raw_dir)
        self.assertTrue(any("没有捕获到文字或图片" in error for error in errors))

    def test_validation_rejects_non_monotonic_catalog_indexes(self):
        titles = ["｜投资随想录｜", "中间章节", "｜投资随想录｜"]
        for chapter_index, (title, catalog_index) in enumerate(
                zip(titles, [0, 1, 0]), start=1):
            save_chapter(
                title, [{"type": "text", "text": "正文"}],
                chapter_index, self.md_dir, self.raw_dir,
                catalog_index=catalog_index)

        errors = validate_export(titles, self.md_dir, self.raw_dir)

        self.assertTrue(any("目录下标不连续" in error for error in errors))

    def test_broken_old_heading_is_detected_as_empty(self):
        with open(os.path.join(self.md_dir, "0001.md"), "w") as chapter_file:
            chapter_file.write("# \n\n正文\n")
        self.assertEqual(get_last_chapter_title(self.md_dir), ("", 1))


class BackupTests(unittest.TestCase):
    def test_existing_book_directory_is_moved_to_backup(self):
        with tempfile.TemporaryDirectory() as root:
            book_dir = os.path.join(root, "book-id")
            os.makedirs(book_dir)
            marker = os.path.join(book_dir, "marker.txt")
            with open(marker, "w") as marker_file:
                marker_file.write("old export")

            backup_dir = backup_existing_export(book_dir, root)

            self.assertFalse(os.path.exists(book_dir))
            self.assertTrue(os.path.isfile(os.path.join(backup_dir, "marker.txt")))
            self.assertIn(os.path.join(root, "_backups"), backup_dir)


if __name__ == "__main__":
    unittest.main()
