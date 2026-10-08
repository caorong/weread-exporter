import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from playwright.async_api import async_playwright

from export_precise import (
    ExportError,
    CANVAS_HOOK,
    DOCUMENT_SNAPSHOT_JS,
    _catalog_index_matches,
    _wait_for_catalog_index,
    backup_existing_export,
    capture_catalog_section,
    capture_verified_section,
    get_last_chapter_title,
    heading_span,
    merge_block_sequences,
    merge_positioned_blocks,
    main,
    next_catalog_index,
    next_catalog_title,
    partition_catalog_sections,
    resolve_catalog_title,
    resolve_chromium_executable,
    save_chapter,
    select_catalog_section_blocks,
    validate_export,
)


class CanvasSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_copied_canvases_keep_document_coordinates_and_clear_old_text(self):
        async with async_playwright() as playwright:
            executable = resolve_chromium_executable(playwright)
            if executable is None and not os.path.exists(playwright.chromium.executable_path):
                self.skipTest("No installed Chromium for the canvas integration test")
            browser = await playwright.chromium.launch(headless=True, executable_path=executable)
            try:
                page = await browser.new_page()
                await page.set_content('''<div class="renderTargetContent"></div>
                    <canvas id="first" width="400" height="200"
                        style="position:absolute;top:100px;left:0;width:200px;height:100px"></canvas>
                    <canvas id="second" width="400" height="200"
                        style="position:absolute;top:500px;left:0;width:200px;height:100px"></canvas>''')
                await page.evaluate(CANVAS_HOOK)
                await page.evaluate('''() => {
                    const buffer = document.createElement('canvas');
                    buffer.width = 400; buffer.height = 200;
                    const ctx = buffer.getContext('2d');
                    ctx.font = '20px sans-serif'; ctx.fillText('旧字', 10, 40);
                    ctx.clearRect(0, 0, 400, 200);
                    ctx.fillText('新字', 10, 40);
                    document.querySelector('#first').getContext('2d').drawImage(buffer, 0, 0);
                    document.querySelector('#second').getContext('2d').drawImage(buffer, 0, 0);
                }''')
                snapshot = await page.evaluate(DOCUMENT_SNAPSHOT_JS)
                chars = snapshot["chars"]
                self.assertEqual([c["t"] for c in chars], ["新字", "新字"])
                self.assertEqual(chars[0]["x"], 5)
                self.assertAlmostEqual(chars[1]["y"] - chars[0]["y"], 400)
                self.assertGreaterEqual(chars[0]["y"], 100)
                self.assertLess(chars[0]["y"], 150)
                await page.evaluate('''() => {
                    const canvas = document.querySelector('#first');
                    canvas.width = canvas.width;
                }''')
                snapshot = await page.evaluate(DOCUMENT_SNAPSHOT_JS)
                self.assertEqual(len(snapshot["chars"]), 1)
                self.assertGreater(snapshot["chars"][0]["y"], 500)
            finally:
                await browser.close()


class CatalogNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bottom_heading_accepts_stale_highlight_and_keeps_target_index(self):
        titles = ["上一节", "男命婚配忌日", "下一章"]
        chars = [{"t": char, "x": i * 20, "y": 255, "visible": True}
                 for i, char in enumerate(titles[1])]
        page = AsyncMock()
        page.evaluate.return_value = list(reversed(chars))
        with patch("export_precise._selected_catalog_index", return_value=0):
            self.assertEqual(await _wait_for_catalog_index(page, 1, titles), 1)

    async def test_bottom_fallback_rejects_missing_clipped_or_ambiguous_heading(self):
        heading = {"t": "目标节", "x": 0, "y": 255, "visible": True}
        cases = [
            ([], ["上一节", "目标节"]),  # Also returned when not at page bottom.
            ([dict(heading, visible=False)], ["上一节", "目标节"]),
            ([dict(heading, t="目标节的正文")], ["上一节", "目标节"]),
            ([heading], ["上一节", "目标节", "目标节"]),
            ([heading, dict(heading, y=400)], ["上一节", "目标节"]),
        ]
        with patch("export_precise._selected_catalog_index", return_value=0):
            for chars, titles in cases:
                with self.subTest(chars=chars, titles=titles):
                    page = AsyncMock()
                    page.evaluate.return_value = chars
                    self.assertFalse(await _catalog_index_matches(page, 1, titles))

    async def test_normal_highlight_needs_no_fallback(self):
        page = AsyncMock()
        with patch("export_precise._selected_catalog_index", return_value=1):
            self.assertTrue(await _catalog_index_matches(page, 1, ["上一节", "目标节"]))
        page.evaluate.assert_not_called()

    async def test_bottom_fallback_accepts_fully_visible_wrapped_title(self):
        page = AsyncMock()
        page.evaluate.return_value = [
            {"t": "附录", "x": 0, "y": 255, "visible": True},
            {"t": "图解流程", "x": 0, "y": 315, "visible": True}]
        with patch("export_precise._selected_catalog_index", return_value=0):
            self.assertTrue(await _catalog_index_matches(page, 1, ["上一节", "附录 图解流程"]))
            page.evaluate.return_value[1]["visible"] = False
            self.assertFalse(await _catalog_index_matches(page, 1, ["上一节", "附录 图解流程"]))

    async def test_unknown_or_later_highlight_does_not_use_fallback(self):
        for index in (-1, 2):
            page = AsyncMock()
            with patch("export_precise._selected_catalog_index", return_value=index):
                self.assertFalse(await _catalog_index_matches(
                    page, 1, ["上一节", "目标节", "下一节"]))
            page.evaluate.assert_not_called()


class CatalogTitleTests(unittest.TestCase):
    def setUp(self):
        self.titles = ["版权信息", "第一章 开始", "尾声"]

    def test_resolves_live_title_and_progress_suffix(self):
        self.assertEqual(resolve_catalog_title(" 第一章  开始 ", self.titles), "第一章 开始")
        self.assertEqual(resolve_catalog_title("尾声当前读到 99%", self.titles), "尾声")
        self.assertEqual(resolve_catalog_title("未知章节", self.titles), "")

    def test_catalog_colon_can_be_replaced_by_heading_line_break(self):
        blocks = [{"type": "text", "text": text} for text in
                  ["附录", "方法索引与思维模型清单", "附录正文"]]
        for separator in ("：", ":"):
            title = "附录" + separator + "方法索引与思维模型清单"
            self.assertEqual(heading_span(blocks, title), (0, 2))
            sections, _ = partition_catalog_sections(blocks, 0, [title])
            self.assertEqual(sections[0], [blocks[2]])
            previous = [{"type": "text", "text": "上一节"},
                        {"type": "text", "text": "上一节正文"}]
            sections, complete = partition_catalog_sections(
                previous + blocks, 0, ["上一节", title])
            self.assertEqual(sections[0], [previous[1]])
            self.assertEqual(sections[1], [blocks[2]])
            self.assertTrue(complete[0])

    def test_colon_heading_still_requires_full_adjacent_title(self):
        title = "附录：方法索引与思维模型清单"
        for texts in (["附录", "方法索引"],
                      ["附录", "其他正文", "方法索引与思维模型清单"],
                      ["附录方法索引与思维模型清单"],
                      ["附录", "方法索引与思维模型清单的介绍"]):
            blocks = [{"type": "text", "text": text} for text in texts]
            self.assertIsNone(heading_span(blocks, title))
        self.assertIsNone(heading_span([
            {"type": "text", "text": "附录"},
            {"type": "img", "src": "figure.png"},
            {"type": "text", "text": "方法索引与思维模型清单"}], title))

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

    def test_wrapped_appendix_title_does_not_jump_to_distant_appendix(self):
        titles = ["附录 八字结构分析图解流程", "命例索引", "其他章节", "附录"]
        blocks = [{"type": "text", "text": text} for text in
                  ["附录", "八字结构分析图解流程", "说明正文", "命例索引", "索引正文"]]
        sections, complete = partition_catalog_sections(blocks, 0, titles)
        self.assertEqual(sections[0], [{"type": "text", "text": "说明正文"}])
        self.assertTrue(complete[0])
        self.assertNotIn(3, sections)

    def test_zero_width_epub_markers_do_not_hide_next_heading(self):
        titles = ["命理学：命和运", "方法论：“黑箱”理论"]
        blocks = [{"type": "text", "text": text} for text in
                  [titles[0], "当前节正文", "方法论：\u200b“黑箱”理论", "下一节正文"]]
        sections, complete = partition_catalog_sections(blocks, 0, titles)
        self.assertEqual(sections[0], [{"type": "text", "text": "当前节正文"}])
        self.assertEqual(sections[1], [{"type": "text", "text": "下一节正文"}])
        self.assertTrue(complete[0])

    def test_decorated_division_label_matches_plain_catalog_title(self):
        blocks = [{"type": "img", "src": "cover.png"},
                  {"type": "text", "text": "｜上编｜"},
                  {"type": "text", "text": "动态篇"}]
        sections, _ = partition_catalog_sections(blocks, 0, ["上编 动态篇"])
        self.assertEqual(sections[0], [blocks[0]])

    def test_cover_above_title_is_preserved_but_previous_prose_is_removed(self):
        image = {"type": "img", "src": "cover.png"}
        heading = {"type": "text", "text": "分册名"}
        sections, _ = partition_catalog_sections([image, heading], 0, ["分册名"])
        self.assertEqual(sections[0], [image])
        sections, _ = partition_catalog_sections(
            [{"type": "text", "text": "上一节正文"}, image, heading], 0, ["分册名"])
        self.assertEqual(sections[0], [])

    def test_document_windows_deduplicate_rendering_but_keep_repeated_prose(self):
        first = [{"type": "text", "text": "同一句", "y": 100},
                 {"type": "text", "text": "同一句", "y": 120}]
        updated = [{"type": "text", "text": "同一句", "y": 104},
                   {"type": "img", "src": "figure.png", "y": 180}]
        merged = merge_positioned_blocks(first, updated)
        self.assertEqual(len(merged), 3)
        self.assertEqual([b["y"] for b in merged], [104, 120, 180])


class SectionCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_render_failure_reloads_once_without_skipping_section(self):
        page = AsyncMock()
        expected = ([{"type": "text", "text": "已定位的正文"}], 1, False)
        with (patch("export_precise.capture_catalog_section", side_effect=[ExportError("未就绪"), expected]) as capture,
              patch("export_precise.goto_catalog_chapter", return_value=(1, ["前一节", "当前节"])),
              patch("export_precise.asyncio.sleep", new_callable=AsyncMock)):
            result = await capture_verified_section(page, 1, ["前一节", "当前节"])
        self.assertEqual(result, expected)
        self.assertEqual(capture.await_count, 2)
        self.assertEqual([call.args[1] for call in capture.await_args_list], [1, 1])
        page.reload.assert_awaited_once()

    async def test_persistent_capture_failure_is_not_swallowed(self):
        page = AsyncMock()
        with (patch("export_precise.capture_catalog_section", side_effect=ExportError("缺少起点")),
              patch("export_precise.goto_catalog_chapter", return_value=(0, ["当前节"])),
              patch("export_precise.asyncio.sleep", new_callable=AsyncMock)):
            with self.assertRaisesRegex(ExportError, "缺少起点"):
                await capture_verified_section(page, 0, ["当前节"])
        page.reload.assert_awaited_once()

    async def capture(self, snapshots, titles):
        page = AsyncMock()
        pending = iter(snapshots)

        async def evaluate(script, *args):
            if script == "() => window.scrollY":
                return snapshots[0]["y"]
            if script == "() => window.__wr_count()":
                return 0
            if "const chars = [], images = []" in script:
                return next(pending)

        page.evaluate.side_effect = evaluate
        with patch("export_precise.asyncio.sleep", new_callable=AsyncMock):
            return await capture_catalog_section(page, 0, titles, start_timeout=0)

    async def test_short_section_discards_unrelated_canvas_prefix(self):
        snapshot = {"y": 20000, "maxY": 20000, "height": 900, "images": [],
                    "chars": [{"t": text, "x": 0, "y": y} for text, y in
                              [("星宫概说", 100), ("前章正文", 150),
                               ("男命婚配忌日", 20255), ("目标正文", 20318)]]}
        blocks, _, title_only = await self.capture([snapshot], ["男命婚配忌日", "下一章"])
        self.assertEqual([b["text"] for b in blocks], ["目标正文"])
        self.assertFalse(title_only)

    async def test_appendix_with_colon_replaced_by_line_break_is_captured(self):
        snapshot = {"y": 405, "maxY": 405, "height": 900, "images": [],
                    "chars": [{"t": text, "x": 0, "y": y} for text, y in
                              [("附录", 558), ("方法索引与思维模型清单", 633),
                               ("附录正文", 966)]]}
        blocks, _, title_only = await self.capture(
            [snapshot], ["附录：方法索引与思维模型清单"])
        self.assertEqual([b["text"] for b in blocks], ["附录正文"])
        self.assertFalse(title_only)

    async def test_missing_target_heading_is_not_silently_saved(self):
        snapshot = {"y": 0, "maxY": 0, "height": 900, "images": [],
                    "chars": [{"t": "其他章节正文", "x": 0, "y": 100}]}
        with self.assertRaisesRegex(ExportError, "未在正文中确认章节起点"):
            await self.capture([snapshot], ["目标章节"])

    async def test_uncaptured_canvas_is_not_misclassified_as_image_only_page(self):
        snapshot = {"y": 0, "maxY": 0, "height": 900, "chars": [],
                    "untrackedCanvases": 1,
                    "images": [{"src": "figure.png", "top": 100, "w": 200, "h": 200}]}
        with self.assertRaisesRegex(ExportError, "未在正文中确认章节起点"):
            await self.capture([snapshot], ["图文章节"])

    async def test_scans_for_lower_images_even_when_next_heading_is_preloaded(self):
        chars = [{"t": text, "x": 0, "y": y} for text, y in
                 [("当前节", 90), ("正文", 200), ("下一节", 1400)]]
        first = {"y": 0, "maxY": 1000, "height": 900, "chars": chars, "images": []}
        second = dict(first, y=675, images=[{"src": "lower.png", "top": 1000, "w": 200, "h": 200}])
        blocks, scans, _ = await self.capture([first, second], ["当前节", "下一节"])
        self.assertEqual(scans, 2)
        self.assertEqual([b["type"] for b in blocks], ["text", "img"])

    async def test_verified_heading_only_is_distinct_from_missing_capture(self):
        snapshot = {"y": 0, "maxY": 0, "height": 900, "images": [],
                    "chars": [{"t": "第九章", "x": 0, "y": 90},
                              {"t": "学历和职业", "x": 0, "y": 130},
                              {"t": "学历分析", "x": 0, "y": 190}]}
        blocks, _, title_only = await self.capture([snapshot], ["第九章 学历和职业", "学历分析"])
        self.assertEqual(blocks, [])
        self.assertTrue(title_only)


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

    def test_validation_accepts_explicitly_verified_heading_only(self):
        save_chapter("分部标题", [], 1, self.md_dir, self.raw_dir,
                     catalog_index=0, title_only=True)
        self.assertEqual(validate_export(["分部标题"], self.md_dir, self.raw_dir), [])

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


class CompletedExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_complete_export_merges_offline_with_working_image_paths(self):
        with tempfile.TemporaryDirectory() as root:
            book_dir = os.path.join(root, "book")
            md_dir, raw_dir = os.path.join(book_dir, "chapters"), os.path.join(book_dir, "raw")
            os.makedirs(md_dir)
            os.makedirs(raw_dir)
            for name, data in [("_catalog.json", ["章节"]),
                               ("_book.json", {"title": "测试书", "author": "作者"})]:
                with open(os.path.join(book_dir, name), "w") as f:
                    json.dump(data, f)
            save_chapter("章节", [{"type": "text", "text": "正文"},
                                   {"type": "img", "src": "https://example.com/figure.png", "w": 200, "h": 200}],
                         1, md_dir, raw_dir, catalog_index=0)
            with (patch("export_precise.run_session", side_effect=AssertionError("must not open browser")) as session,
                  patch("export_precise.download_all_images", return_value=1)):
                self.assertTrue(await main("book", output_root=root))
            session.assert_not_called()
            with open(os.path.join(root, "测试书.md")) as f:
                self.assertIn("](book/images/ch0001_img01.png)", f.read())
            with open(os.path.join(md_dir, "0001.md")) as f:
                self.assertIn("](../images/ch0001_img01.png)", f.read())

    async def test_old_capture_version_cannot_take_offline_completion_shortcut(self):
        with tempfile.TemporaryDirectory() as root:
            book_dir = os.path.join(root, "book")
            md_dir, raw_dir = os.path.join(book_dir, "chapters"), os.path.join(book_dir, "raw")
            os.makedirs(md_dir)
            os.makedirs(raw_dir)
            save_chapter("章节", [{"type": "text", "text": "旧正文"}], 1, md_dir, raw_dir)
            for name, data in [("_catalog.json", ["章节"]),
                               ("_book.json", {"title": "测试书"}),
                               ("raw/0001.json", {"title": "章节", "text_len": 3, "images": []})]:
                with open(os.path.join(book_dir, name), "w") as f:
                    json.dump(data, f)
            with patch("export_precise.run_session", side_effect=ExportError("需要重新核对")) as session:
                self.assertFalse(await main("book", output_root=root))
            session.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
