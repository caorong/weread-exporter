#!/usr/bin/env python3
"""
微信读书导出 — 精确图文版 v3

逐页捕获：每翻一页，抓当前视口内的 canvas 文字 + 视口内图片，
按屏幕 y 坐标把文字行和图片交错排序，图片精确落在对应段落之间。
双页拆分(左页→右页)，按目录精确切分并自动续传。
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
import urllib.request

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

USER_DATA_DIR = os.path.join("cache", "browser_profile")


class ExportError(RuntimeError):
    pass


def resolve_chromium_executable(playwright):
    """Use an installed browser when Playwright's bundled Chromium is absent."""
    bundled = playwright.chromium.executable_path
    if os.path.exists(bundled):
        return None

    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    candidates = [
        configured,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    return next((path for path in candidates if path and os.path.isfile(path)), None)

CANVAS_HOOK = """
(function() {
    window.__wr_chars = [];
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && text.trim())
            window.__wr_chars.push({t: text, x: Math.round(x*10)/10, y: Math.round(y*10)/10});
        return origFill.apply(this, arguments);
    };
    window.__wr_reset = function() { window.__wr_chars = []; };
    window.__wr_count = function() { return window.__wr_chars.length; };
})();
"""

# 当前视口内可见的书籍插图，带屏幕坐标
VIEWPORT_IMGS_JS = """
() => {
    const H = window.innerHeight, W = window.innerWidth, out = [];
    document.querySelectorAll('img[class*="wr_readerImage"]').forEach(i => {
        const src = i.src || i.getAttribute('data-src') || '';
        if (!src.includes('res.weread.qq.com/wrepub')) return;
        const r = i.getBoundingClientRect();
        if (r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < H &&
            r.right > 0 && r.left < W &&
            getComputedStyle(i).visibility !== 'hidden' &&
            getComputedStyle(i).display !== 'none') {
            out.push({src, top: Math.round(r.top), left: Math.round(r.left),
                      w: i.naturalWidth||i.width, h: i.naturalHeight||i.height});
        }
    });
    return out;
}
"""

# reader 的两个 canvas 的屏幕位置
CANVAS_RECTS_JS = """
() => Array.from(document.querySelectorAll('canvas')).map(c => {
    const r = c.getBoundingClientRect();
    return {top: r.top, left: r.left, w: Math.round(r.width), h: Math.round(r.height)};
}).filter(r => r.h > 300)
"""

# Canvas 已绘制但滚动到内部目录锚点时不会再次触发 fillText。
# 微信读书保留的绝对定位字符层可作为这种场景的兜底数据源。
DOM_CHARS_JS = """
() => Array.from(document.querySelectorAll('.renderTargetContent .wr_absolute'))
    .map(el => {
        const r = el.getBoundingClientRect();
        return {t: el.textContent || '', x: r.left, y: r.top};
    })
    .filter(c => c.t.trim())
"""

MEASURE_RE = re.compile(r'^[a-zA-Z0-9`~!@#$%^&*()\-_=+\[\]{}|;:\',<.>/?\\"\s]+$')
SENTENCE_END = set("。！？；：」）】》…—")


def split_spread(chars):
    """双页拆分：返回 [左页chars, 右页chars] 或 [单页chars]"""
    if len(chars) < 20:
        return [chars]
    singles = [(i, c) for i, c in enumerate(chars) if len(c["t"]) == 1]
    if len(singles) < 10:
        return [chars]
    for j in range(1, len(singles)):
        if singles[j - 1][1]["y"] > 400 and singles[j][1]["y"] < 200:
            return [chars[:singles[j][0]], chars[singles[j][0]:]]
    return [chars]


def chars_to_lines(chars):
    """把单页字符按 y 分行，返回 [{y, text}]（未合并段落）"""
    real = [c for c in chars if len(c["t"]) == 1 or not MEASURE_RE.match(c["t"])]
    if not real:
        return []
    rows = {}
    for c in real:
        y_key = round(c["y"] / 3) * 3
        rows.setdefault(y_key, []).append(c)
    lines = []
    for yk in sorted(rows):
        line = "".join(c["t"] for c in sorted(rows[yk], key=lambda c: c["x"]))
        if line.strip():
            lines.append({"y": yk, "text": line.strip()})
    return lines


def build_page_blocks(chars, images, canvas_rects, seen_imgs):
    """把一次渲染(可能双页)拆成有序块列表: [{type:'text'/'img', ...}]
       文字行和图片按屏幕 y 交错；左页整页在前，右页在后。"""
    blocks = []
    pages = split_spread(chars)

    # 判定左右 canvas
    rects = sorted(canvas_rects, key=lambda r: r["left"])
    left_rect = rects[0] if rects else {"top": 0, "left": 0}
    right_rect = rects[1] if len(rects) > 1 else left_rect
    mid_x = (left_rect["left"] + right_rect["left"]) / 2 + 180 if len(rects) > 1 else 99999

    # 图片按左右分组
    left_imgs = [im for im in images if im["left"] < mid_x]
    right_imgs = [im for im in images if im["left"] >= mid_x]

    def emit_page(page_chars, page_rect, page_imgs):
        lines = chars_to_lines(page_chars)
        items = []
        for ln in lines:
            items.append(("text", page_rect["top"] + ln["y"], ln["text"]))
        for im in page_imgs:
            if im["src"] in seen_imgs:
                continue
            items.append(("img", im["top"], im))
        items.sort(key=lambda t: t[1])
        for typ, _y, payload in items:
            if typ == "text":
                blocks.append({"type": "text", "text": payload})
            else:
                seen_imgs.add(payload["src"])
                blocks.append({"type": "img", "src": payload["src"],
                                "w": payload["w"], "h": payload["h"]})

    if len(pages) == 2:
        emit_page(pages[0], left_rect, left_imgs)
        emit_page(pages[1], right_rect, right_imgs)
    else:
        # 单页：图片全归这页，仍按 y 排
        emit_page(pages[0], left_rect, left_imgs + right_imgs)
    return blocks


def build_dom_blocks(chars, images, seen_imgs):
    items = [("text", line["y"], line["text"]) for line in chars_to_lines(chars)]
    for image in images:
        if image["src"] not in seen_imgs:
            items.append(("img", image["top"], image))
    items.sort(key=lambda item: item[1])

    blocks = []
    for block_type, _y, payload in items:
        if block_type == "text":
            blocks.append({"type": "text", "text": payload})
        else:
            seen_imgs.add(payload["src"])
            blocks.append({"type": "img", "src": payload["src"],
                           "w": payload["w"], "h": payload["h"]})
    return blocks


def img_filename(url, ch_idx, seq):
    ext = "jpg"
    m = re.search(r'\.(jpg|jpeg|png|gif|webp)', url.lower())
    if m:
        ext = m.group(1).replace("jpeg", "jpg")
    return f"ch{ch_idx:04d}_img{seq:02d}.{ext}"


def render_chapter_md(ch_title, blocks, ch_idx):
    """把有序块渲染成 Markdown：文字行合并成段落，图片就地插入"""
    out = [f"# {ch_title}\n"]
    para = []
    img_records = []
    img_seq = 0

    def flush_para():
        nonlocal para
        if not para:
            return
        # 合并 canvas 断行为自然段：上一行不以句末标点结尾则接续
        merged = []
        for line in para:
            if line == ch_title:
                continue
            if merged and merged[-1] and merged[-1][-1] not in SENTENCE_END:
                merged[-1] += line
            else:
                merged.append(line)
        for m in merged:
            if m.strip():
                out.append(m.strip())
        para = []

    for b in blocks:
        if b["type"] == "text":
            para.append(b["text"])
        else:
            flush_para()
            img_seq += 1
            fname = img_filename(b["src"], ch_idx, img_seq)
            out.append(f"![图](images/{fname})")
            img_records.append({"url": b["src"], "file": fname})
    flush_para()

    body = "\n\n".join(out) + "\n"
    return body, img_records


async def wait_stable(page, timeout=8):
    """等页面渲染稳定，返回稳定后的字符数"""
    last = -1
    for _ in range(int(timeout / 0.5)):
        c = await page.evaluate("() => window.__wr_count()")
        if c == last:
            return c
        last = c
        await asyncio.sleep(0.5)
    return last


def get_last_chapter_title(md_dir):
    if not os.path.exists(md_dir):
        return None, 0
    files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    if not files:
        return None, 0
    idx = int(files[-1].replace(".md", ""))
    with open(os.path.join(md_dir, files[-1])) as f:
        title = f.readline().strip().removeprefix("#").strip()
    return title, idx


def load_catalog_titles(catalog_path):
    try:
        with open(catalog_path) as f:
            titles = json.load(f)
        return [str(title).strip() for title in titles if str(title).strip()]
    except Exception:
        return []


def canonical_title(title):
    return re.sub(r"\s+", "", title or "")


def resolve_catalog_title(candidate, catalog_titles):
    """Return the exact catalog spelling for a title read from the live DOM."""
    value = canonical_title(candidate)
    if not value:
        return ""
    if not catalog_titles:
        return str(candidate).strip()

    exact = {canonical_title(title): title for title in catalog_titles}
    if value in exact:
        return exact[value]

    # Selected catalog rows may append progress text such as "当前读到 99%".
    for title in sorted(catalog_titles, key=lambda item: len(canonical_title(item)), reverse=True):
        if value.startswith(canonical_title(title)):
            return title
    return ""


def _catalog_indexes_for_title(title, catalog_titles):
    resolved = resolve_catalog_title(title, catalog_titles)
    if not resolved:
        return []
    key = canonical_title(resolved)
    return [
        index for index, catalog_title in enumerate(catalog_titles)
        if canonical_title(catalog_title) == key
    ]


def next_catalog_index(catalog_titles, resume_after_index=None,
                       resume_after_title=None):
    if not catalog_titles:
        raise ExportError("目录为空，无法确定导出范围")

    if resume_after_index is not None:
        if (isinstance(resume_after_index, bool) or
                not isinstance(resume_after_index, int)):
            raise ExportError(f"断点目录下标无效: {resume_after_index!r}")
        if not 0 <= resume_after_index < len(catalog_titles):
            raise ExportError(
                f"断点目录下标 {resume_after_index} 超出当前目录范围 "
                f"0..{len(catalog_titles) - 1}；现有导出可能已损坏，"
                "请使用 --restart 重新导出")
        if resume_after_title:
            expected = catalog_titles[resume_after_index]
            if canonical_title(expected) != canonical_title(resume_after_title):
                raise ExportError(
                    f"断点不一致: 第 {resume_after_index + 1} 个目录项应为"
                    f"「{expected}」，现有文件为「{resume_after_title}」；"
                    "请使用 --restart 重新导出")
        next_index = resume_after_index + 1
        return next_index if next_index < len(catalog_titles) else None

    if not resume_after_title:
        return 0

    matches = _catalog_indexes_for_title(resume_after_title, catalog_titles)
    if not matches:
        raise ExportError(f"断点章节不在当前目录中: {resume_after_title!r}")
    if len(matches) > 1:
        positions = "、".join(str(index + 1) for index in matches)
        raise ExportError(
            f"断点章节「{resume_after_title}」在目录中出现多次"
            f"（第 {positions} 项），无法仅凭标题恢复")
    next_index = matches[0] + 1
    return next_index if next_index < len(catalog_titles) else None


def next_catalog_title(catalog_titles, resume_after_title=None):
    index = next_catalog_index(
        catalog_titles, resume_after_title=resume_after_title)
    return catalog_titles[index] if index is not None else None


def partition_catalog_sections(blocks, current_index, catalog_titles):
    """Split rendered content into catalog-indexed sections."""
    if not 0 <= current_index < len(catalog_titles):
        raise ExportError(f"当前目录下标无效: {current_index}")

    indexes_by_key = {}
    for index in range(current_index, len(catalog_titles)):
        key = canonical_title(catalog_titles[index])
        indexes_by_key.setdefault(key, []).append(index)

    sections = {current_index: []}
    complete = {current_index: False}
    active_index = current_index
    saw_current_marker = False

    for block in blocks:
        marker_index = None
        if block.get("type") == "text":
            candidates = indexes_by_key.get(
                canonical_title(block.get("text", "")), [])
            if (not saw_current_marker and active_index == current_index and
                    current_index in candidates):
                marker_index = current_index
            else:
                marker_index = next(
                    (index for index in candidates if index > active_index), None)

        if marker_index is not None:
            if marker_index == current_index and not saw_current_marker:
                sections[current_index] = []
                active_index = current_index
                saw_current_marker = True
                continue
            if marker_index != active_index:
                complete[active_index] = True
                active_index = marker_index
                sections.setdefault(active_index, [])
                complete.setdefault(active_index, False)
                continue

        sections.setdefault(active_index, []).append(block)
    return sections, complete


def select_catalog_section_blocks(blocks, current_title, catalog_titles,
                                  current_index=None):
    """Keep only the current catalog section from an oversized rendered chunk."""
    if current_index is None:
        matches = _catalog_indexes_for_title(current_title, catalog_titles)
        if not matches:
            raise ExportError(f"当前章节不在目录中: {current_title!r}")
        if len(matches) > 1:
            raise ExportError(
                f"章节「{current_title}」在目录中出现多次，"
                "必须提供 current_index")
        current_index = matches[0]
    elif (not 0 <= current_index < len(catalog_titles) or
          canonical_title(catalog_titles[current_index]) !=
          canonical_title(current_title)):
        raise ExportError(
            f"当前章节与目录下标不一致: {current_title!r}, {current_index}")

    sections, complete = partition_catalog_sections(
        blocks, current_index, catalog_titles)
    return sections.get(current_index, []), complete.get(current_index, False)


def merge_block_sequences(existing, incoming):
    """Merge overlapping virtualized DOM windows without duplicating lines."""
    if not existing:
        return list(incoming)
    if not incoming:
        return list(existing)

    def signature(block):
        if block.get("type") == "text":
            return "text", block.get("text", "")
        return "img", block.get("src", "")

    left = [signature(block) for block in existing]
    right = [signature(block) for block in incoming]
    max_overlap = min(len(left), len(right))
    overlap = 0
    for size in range(max_overlap, 0, -1):
        if left[-size:] == right[:size]:
            overlap = size
            break
    return list(existing) + list(incoming[overlap:])


async def _title(page, catalog_titles=None):
    candidates = await page.evaluate("""() => [
        document.querySelector(
            '.readerCatalog_list_item_selected .readerCatalog_list_item_title_text')?.textContent,
        document.querySelector('.readerCatalog_list_item_selected')?.textContent,
        document.querySelector('.readerTopBar_title_chapter')?.textContent,
        document.querySelector('.renderTargetPageInfo_header_chapterTitle')?.textContent,
    ].map(text => text?.trim() || '').filter(Boolean)""")
    for candidate in candidates:
        title = resolve_catalog_title(candidate, catalog_titles or [])
        if title:
            return title
    return ""


async def fetch_book_title(page):
    info = await page.evaluate("""() => {
        const title = document.querySelector('.readerCatalog_bookInfo_title_txt, .bookInfo_right_header_title')
            ?.textContent?.trim() || document.title.replace(/-.*$/, '').trim();
        const author = document.querySelector('.readerCatalog_bookInfo_author, .bookInfo_author a')
            ?.textContent?.trim() || '';
        return {title, author};
    }""")
    return info.get("title", "未知"), info.get("author", "")


async def _catalog_is_open(page):
    return await page.evaluate("""() => Array.from(
        document.querySelectorAll('.readerCatalog_list_item')).some(el => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
        })""")


async def _selected_catalog_index(page):
    return await page.evaluate("""() => {
        const items = Array.from(document.querySelectorAll('.readerCatalog_list_item'));
        const selected = document.querySelector('.readerCatalog_list_item_selected');
        if (!selected) return -1;
        return items.findIndex(item => item === selected || item.contains(selected));
    }""")


async def _open_catalog(page):
    if not await _catalog_is_open(page):
        await page.click("button.readerControls_item.catalog", timeout=5000)
    await page.locator(".readerCatalog_list_item").first.wait_for(
        state="visible", timeout=5000)
    await asyncio.sleep(0.35)


async def _wait_for_catalog_closed(page, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not await _catalog_is_open(page):
            return
        await asyncio.sleep(0.1)
    raise ExportError("目录点击后未能关闭")


async def _wait_for_catalog_index(page, expected_index, catalog_titles, timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        current_index = await _selected_catalog_index(page)
        if current_index == expected_index:
            return current_index
        await asyncio.sleep(0.2)
    current_index = await _selected_catalog_index(page)
    expected_title = catalog_titles[expected_index]
    if 0 <= current_index < len(catalog_titles):
        actual = f"第 {current_index + 1} 项「{catalog_titles[current_index]}」"
    else:
        actual_title = await _title(page, catalog_titles)
        actual = f"「{actual_title or '(无法识别)'}」"
    raise ExportError(
        f"目录跳转未生效: 期望第 {expected_index + 1} 项"
        f"「{expected_title}」，实际 {actual}")


async def _click_catalog_index(page, target_index, catalog_titles):
    if not 0 <= target_index < len(catalog_titles):
        raise ExportError(f"目标目录下标无效: {target_index}")
    await _open_catalog(page)
    await page.evaluate("() => window.__wr_reset()")
    await page.locator(".readerCatalog_list_item").nth(target_index).click(timeout=5000)
    await _wait_for_catalog_closed(page)
    await _wait_for_catalog_index(page, target_index, catalog_titles)
    await wait_stable(page)


async def goto_catalog_chapter(page, resume_after_title=None,
                               resume_after_index=None, catalog_path=None):
    """Open the catalog and navigate to the first chapter not yet exported."""
    initial_index = await _selected_catalog_index(page)
    await _open_catalog(page)
    if initial_index < 0:
        initial_index = await _selected_catalog_index(page)

    titles = await page.evaluate(r"""() => Array.from(
        document.querySelectorAll('.readerCatalog_list_item')).map(el =>
            el.querySelector('.readerCatalog_list_item_title_text')?.textContent?.trim() ||
            el.textContent.trim().replace(/当前读到\s*\d+%?$/, '').trim()
        ).filter(Boolean)""")
    if catalog_path:
        with open(catalog_path, "w") as f:
            json.dump(titles, f, ensure_ascii=False)

    target_index = next_catalog_index(
        titles, resume_after_index=resume_after_index,
        resume_after_title=resume_after_title)
    if target_index is None:
        if await _catalog_is_open(page):
            await page.keyboard.press("Escape")
        return None, titles

    # Clicking the already selected row does not repaint Canvas. Visit a neighbor
    # first so the final target click always produces a fresh fillText sequence.
    if initial_index == target_index and len(titles) > 1:
        neighbor_index = target_index - 1 if target_index > 0 else 1
        await _click_catalog_index(page, neighbor_index, titles)

    await _click_catalog_index(page, target_index, titles)

    print(f"  ✅ 已定位到待导出章节:"
          f"第 {target_index + 1} 项「{titles[target_index]}」")
    return target_index, titles


def save_chapter(ch_title, blocks, ch_idx, md_dir, raw_dir,
                 catalog_index=None):
    if not ch_title.strip():
        raise ExportError("拒绝保存标题为空的章节")
    body, img_records = render_chapter_md(ch_title, blocks, ch_idx)
    text_len = sum(len(b["text"]) for b in blocks if b["type"] == "text")
    with open(os.path.join(md_dir, f"{ch_idx:04d}.md"), "w") as f:
        f.write(body)
    record = {"title": ch_title, "images": img_records, "text_len": text_len}
    if catalog_index is not None:
        record["catalog_index"] = catalog_index
    with open(os.path.join(raw_dir, f"{ch_idx:04d}.json"), "w") as f:
        json.dump(record, f, ensure_ascii=False)
    return text_len, img_records


def validate_export(catalog_titles, md_dir, raw_dir):
    errors = []
    expected = [canonical_title(title) for title in catalog_titles]
    md_files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    raw_files = sorted(f for f in os.listdir(raw_dir) if f.endswith(".json"))

    if [f.replace(".md", "") for f in md_files] != [f.replace(".json", "") for f in raw_files]:
        errors.append("chapters/ 与 raw/ 的章节文件编号不一致")

    actual_display_titles = []
    actual_titles = []
    catalog_index_mismatch = None
    for expected_index, filename in enumerate(raw_files):
        try:
            with open(os.path.join(raw_dir, filename)) as f:
                record = json.load(f)
                title = str(record.get("title", "")).strip()
        except Exception as exc:
            errors.append(f"无法读取 {filename}: {exc}")
            continue
        if not title:
            errors.append(f"{filename} 的章节标题为空")
        if not record.get("text_len", 0) and not record.get("images", []):
            errors.append(f"{filename}「{title or '(无标题)'}」没有捕获到文字或图片")
        catalog_index = record.get("catalog_index")
        if (catalog_index is not None and catalog_index != expected_index and
                catalog_index_mismatch is None):
            catalog_index_mismatch = (
                filename, expected_index, catalog_index)
        actual_display_titles.append(title)
        actual_titles.append(canonical_title(title))

    if catalog_index_mismatch:
        filename, expected_index, actual_index = catalog_index_mismatch
        errors.append(
            f"目录下标不连续: {filename} 期望 {expected_index}，"
            f"实际 {actual_index}")

    if actual_titles != expected:
        mismatch = next((i for i, pair in enumerate(zip(actual_titles, expected))
                         if pair[0] != pair[1]), min(len(actual_titles), len(expected)))
        expected_title = catalog_titles[mismatch] if mismatch < len(catalog_titles) else "(无)"
        actual_title = actual_display_titles[mismatch] if mismatch < len(actual_titles) else "(无)"
        errors.append(
            f"目录覆盖不完整: 期望 {len(expected)} 章，实际 {len(actual_titles)} 章；"
            f"首个差异位于第 {mismatch + 1} 章，期望「{expected_title}」，实际「{actual_title}」")
    return errors


def backup_existing_export(book_dir, output_root):
    if not os.path.exists(book_dir):
        return None
    backup_root = os.path.join(output_root, "_backups")
    os.makedirs(backup_root, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(backup_root, f"{os.path.basename(book_dir)}-{stamp}")
    suffix = 1
    while os.path.exists(backup_dir):
        backup_dir = os.path.join(
            backup_root, f"{os.path.basename(book_dir)}-{stamp}-{suffix}")
        suffix += 1
    shutil.move(book_dir, backup_dir)
    return backup_dir


async def ensure_vertical_reading_mode(page, headless=False):
    """Require normal (vertical scrolling) mode before reading the catalog."""
    vertical = page.locator("button.readerControls_item.isNormalReader")
    horizontal = page.locator("button.readerControls_item.isHorizontalReader")
    if await horizontal.count():
        message = (
            "当前为横向翻页模式。导出前必须切换为纵向（上下滚动）阅读模式，"
            "否则可能出现目录跳转失败或正文缺失。")
        if headless:
            raise ExportError(message + "请先用普通模式运行，在浏览器中切换。")
        print(f"\n  ⚠️  {message}", flush=True)
        print("  请在当前浏览器的阅读器侧边工具栏点击阅读模式切换按钮；"
              "切换后自动继续，最多等待 2 分钟。", flush=True)
        try:
            await vertical.wait_for(state="visible", timeout=120000)
        except PlaywrightTimeoutError as exc:
            raise ExportError("等待切换纵向阅读模式超时，请重新运行并完成模式切换。") from exc
        await asyncio.sleep(3)
    if not await vertical.count():
        raise ExportError("无法识别阅读模式，请确认书籍已加载，并使用纵向（上下滚动）阅读模式。")
    print("  ✅ 已确认纵向阅读模式")


async def run_session(book_id, md_dir, raw_dir, start_idx, seen_imgs,
                      resume_after_title=None, resume_after_index=None,
                      catalog_path=None, headless=False):
    async with async_playwright() as p:
        executable_path = resolve_chromium_executable(p)
        launch_options = {
            "headless": headless,
            "viewport": {"width": 1200, "height": 900},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if executable_path:
            launch_options["executable_path"] = executable_path
            print(f"  🌐 使用系统浏览器: {executable_path}")
        ctx = await p.chromium.launch_persistent_context(USER_DATA_DIR, **launch_options)
        try:
            login_page = await ctx.new_page()
            await login_page.goto("https://weread.qq.com/web/shelf", timeout=30000)
            await asyncio.sleep(3)
            if "login" in login_page.url.lower():
                if headless:
                    raise ExportError("登录已失效；请先用普通模式运行并扫码登录")
                print("\n  ⚠️  请扫码登录微信读书")
                for _ in range(120):
                    await asyncio.sleep(5)
                    if "login" not in login_page.url.lower():
                        print("  ✅ 登录成功")
                        break
                else:
                    raise ExportError("等待登录超时")
            else:
                print("  ✅ 已登录")
            await login_page.close()

            page = await ctx.new_page()
            await page.add_init_script(CANVAS_HOOK)
            print("\n  打开阅读器...")
            await page.goto(f"https://weread.qq.com/web/reader/{book_id}",
                            wait_until="networkidle", timeout=30000)
            await asyncio.sleep(5)

            await ensure_vertical_reading_mode(page, headless=headless)
            book_title, book_author = await fetch_book_title(page)
            target_index, catalog_titles = await goto_catalog_chapter(
                page, resume_after_title=resume_after_title,
                resume_after_index=resume_after_index,
                catalog_path=catalog_path)
            if target_index is None:
                return book_title, book_author, 0, 0, start_idx, True

            catalog_levels = await page.evaluate(r"""() => Array.from(
                document.querySelectorAll('.readerCatalog_list_item')).map(el => {
                    const inner = el.querySelector('.readerCatalog_list_item_inner');
                    const match = inner?.className.match(/readerCatalog_list_item_level_(\d+)/);
                    return match ? Number(match[1]) : 1;
                })""")
            if len(catalog_levels) != len(catalog_titles):
                catalog_levels = [1] * len(catalog_titles)

            current_index = await _selected_catalog_index(page)
            if current_index != target_index:
                actual_title = (
                    catalog_titles[current_index]
                    if 0 <= current_index < len(catalog_titles) else "(无法识别)")
                raise ExportError(
                    f"起始章节校验失败: 期望第 {target_index + 1} 项"
                    f"「{catalog_titles[target_index]}」，实际第 {current_index + 1} 项"
                    f"「{actual_title}」")
            if target_index != start_idx - 1:
                raise ExportError(
                    f"断点编号不一致: 文件将从 {start_idx:04d} 开始，"
                    f"但待导出的是第 {target_index + 1} 个目录项")

            current_chapter = catalog_titles[current_index]

            print(f"  📖 {book_title} — {book_author}")
            print(f"  会话开始: 第 {current_index + 1} 项"
                  f"「{current_chapter}」\n")

            ch_idx = start_idx
            ch_blocks = []
            total_chars = 0
            chapters_this_session = 0
            prefetched_blocks = {}
            prefetched_complete = {}

            def cache_future_sections(sections, complete):
                for section_index, blocks in sections.items():
                    if section_index == current_index:
                        continue
                    prefetched_blocks[section_index] = merge_block_sequences(
                        prefetched_blocks.get(section_index, []), blocks)
                    prefetched_complete[section_index] = (
                        prefetched_complete.get(section_index, False) or
                        complete.get(section_index, False))

            async def capture_current_page():
                """抓当前页的有序块，累加到 ch_blocks；返回是否有新内容"""
                nonlocal ch_blocks
                await asyncio.sleep(0.3)
                chars = await page.evaluate("() => window.__wr_chars")
                before = len(ch_blocks)
                new_blocks = prefetched_blocks.pop(current_index, [])
                section_complete = prefetched_complete.pop(current_index, False)

                if chars:
                    rects = await page.evaluate(CANVAS_RECTS_JS)
                    imgs = await page.evaluate(VIEWPORT_IMGS_JS)
                    scratch_seen = set(seen_imgs)
                    rendered_blocks = build_page_blocks(
                        chars, imgs, rects, scratch_seen)
                    sections, complete = partition_catalog_sections(
                        rendered_blocks, current_index, catalog_titles)
                    new_blocks = merge_block_sequences(
                        new_blocks, sections.get(current_index, []))
                    section_complete = (
                        section_complete or complete.get(current_index, False))
                    cache_future_sections(sections, complete)
                current_level = catalog_levels[current_index]
                needs_dom_fallback = (
                    not section_complete and (not chars or current_level > 1))
                if needs_dom_fallback:
                    initial_scroll = await page.evaluate("() => window.scrollY")
                    scan_section = current_index < len(catalog_titles) - 1
                    try:
                        for _ in range(20):
                            dom_chars = await page.evaluate(DOM_CHARS_JS)
                            imgs = await page.evaluate(VIEWPORT_IMGS_JS)
                            scratch_seen = set(seen_imgs)
                            scratch_seen.update(
                                block["src"] for block in new_blocks
                                if block.get("type") == "img")
                            window_blocks = build_dom_blocks(
                                dom_chars, imgs, scratch_seen)
                            sections, complete = partition_catalog_sections(
                                window_blocks, current_index, catalog_titles)
                            current_window = sections.get(current_index, [])
                            found_next = complete.get(current_index, False)
                            cache_future_sections(sections, complete)
                            visible_index = await _selected_catalog_index(page)
                            if visible_index != current_index and not found_next:
                                break
                            new_blocks = merge_block_sequences(
                                new_blocks, current_window)
                            if found_next or not scan_section:
                                break

                            scroll_state = await page.evaluate("""() => ({
                                y: window.scrollY,
                                maxY: Math.max(0, document.documentElement.scrollHeight - innerHeight),
                                step: Math.max(400, Math.floor(innerHeight * 0.75)),
                            })""")
                            if scroll_state["y"] >= scroll_state["maxY"] - 2:
                                break
                            next_y = min(
                                scroll_state["maxY"],
                                scroll_state["y"] + scroll_state["step"])
                            await page.evaluate("y => window.scrollTo(0, y)", next_y)
                            await asyncio.sleep(0.4)
                    finally:
                        current_scroll = await page.evaluate("() => window.scrollY")
                        if abs(current_scroll - initial_scroll) > 1:
                            await page.evaluate(
                                "y => window.scrollTo(0, y)", initial_scroll)
                            await asyncio.sleep(0.3)
                            await _wait_for_catalog_index(
                                page, current_index, catalog_titles, timeout=3)

                for block in new_blocks:
                    if block.get("type") == "img":
                        seen_imgs.add(block["src"])
                ch_blocks = merge_block_sequences(ch_blocks, new_blocks)
                return len(ch_blocks) > before

            while True:
                expected_index = target_index + chapters_this_session
                if expected_index >= len(catalog_titles):
                    raise ExportError(
                        f"导出章节数超过目录剩余项数 "
                        f"{len(catalog_titles) - target_index}，已中止")
                if current_index != expected_index:
                    raise ExportError(
                        f"目录下标未单调前进: 期望 {expected_index}，"
                        f"实际 {current_index}，已中止")

                page_num = 1 if await capture_current_page() else 0
                n, imgs = save_chapter(
                    current_chapter, ch_blocks, ch_idx, md_dir, raw_dir,
                    catalog_index=current_index)
                total_chars += n
                note = f" +{len(imgs)}图" if imgs else ""
                end_note = (
                    " [全书末尾]"
                    if current_index == len(catalog_titles) - 1 else "")
                print(f"  [{ch_idx:4d}] {current_chapter[:32]:32s} "
                      f"{n:6d}字 ({page_num}页){note}{end_note}")
                chapters_this_session += 1
                ch_idx += 1

                if current_index == len(catalog_titles) - 1:
                    reached_end = True
                    break

                next_index = current_index + 1
                ch_blocks = []
                await _click_catalog_index(page, next_index, catalog_titles)
                selected_index = await _selected_catalog_index(page)
                if selected_index != next_index:
                    raise ExportError(
                        f"章节定位失败: 期望目录下标 {next_index}，"
                        f"实际 {selected_index}")
                current_index = selected_index
                current_chapter = catalog_titles[current_index]

            return (book_title, book_author, chapters_this_session,
                    total_chars, ch_idx, reached_end)
        finally:
            await ctx.close()


def download_all_images(raw_dir, img_dir):
    os.makedirs(img_dir, exist_ok=True)
    tasks = []
    for jf in sorted(os.listdir(raw_dir)):
        if jf.endswith(".json"):
            for img in json.load(open(os.path.join(raw_dir, jf))).get("images", []):
                tasks.append((img["url"], img["file"]))
    if not tasks:
        print("  (无图片)"); return 0
    print(f"\n  下载 {len(tasks)} 张图片...")
    ok = 0
    for url, fname in tasks:
        fp = os.path.join(img_dir, fname)
        if os.path.exists(fp) and os.path.getsize(fp) > 1000:
            ok += 1; continue
        try:
            req = urllib.request.Request(url, headers={
                "Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            if len(raw) > 500:
                open(fp, "wb").write(raw); ok += 1
                if ok % 20 == 0:
                    print(f"    {ok}/{len(tasks)}...")
        except Exception as e:
            print(f"    ⚠️  {fname} 失败: {e}")
    print(f"  ✅ 图片下载完成 {ok}/{len(tasks)}")
    return ok


async def main(book_id, output_root="output", restart=False, headless=False):
    print("=" * 60)
    print("  weread-exporter — 精确图文导出 v3")
    print("=" * 60)
    print("  使用前请确保微信读书已切换为纵向（上下滚动）阅读模式。", flush=True)
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    book_dir = os.path.join(output_root, book_id)
    if restart:
        backup_dir = backup_existing_export(book_dir, output_root)
        if backup_dir:
            print(f"  ♻️  旧导出已备份到: {backup_dir}")
    md_dir = os.path.join(book_dir, "chapters")
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")
    for d in (md_dir, raw_dir, img_dir):
        os.makedirs(d, exist_ok=True)

    seen_imgs = set()
    for jf in os.listdir(raw_dir):
        if jf.endswith(".json"):
            for img in json.load(open(os.path.join(raw_dir, jf))).get("images", []):
                seen_imgs.add(img["url"])

    catalog_path = os.path.join(book_dir, "_catalog.json")
    book_title = book_author = ""
    session = 0
    no_progress_sessions = 0
    while True:
        session += 1
        last_title, last_idx = get_last_chapter_title(md_dir)
        if last_idx > 0 and not last_title:
            print("\n  ❌ 现有断点的章节标题为空，不能安全续传。")
            print("     请使用 --restart 重新导出；旧产物会自动备份，不会删除。")
            return False
        start_idx = last_idx + 1 if last_idx > 0 else 1
        print(f"\n--- 会话 {session} ---")
        print(f"  上次: {last_title or '(无)'}, 编号: {last_idx}")
        try:
            title, author, added, chars_added, _end_idx, reached_end = await run_session(
                book_id, md_dir, raw_dir, start_idx, seen_imgs,
                resume_after_title=last_title,
                resume_after_index=last_idx - 1 if last_idx > 0 else None,
                catalog_path=catalog_path,
                headless=headless)
        except ExportError as exc:
            print(f"\n  ❌ 导出中止: {exc}")
            return False
        if title: book_title = title
        if author: book_author = author
        print(f"\n  本次: +{added} 章, +{chars_added:,} 字")
        if reached_end:
            print("\n  ✅ 已到全书最后一章，开始完整性校验。")
            break
        if added == 0:
            no_progress_sessions += 1
        else:
            no_progress_sessions = 0
        if no_progress_sessions >= 2:
            print("\n  ❌ 连续两个会话没有新增完整章节，未到全书末尾。")
            print("     已保留之前完整章节，但不会生成“全书导出完成”结果。")
            return False
        print("  3 秒后从下一个未完成章节重开...")
        await asyncio.sleep(3)

    catalog_titles = load_catalog_titles(catalog_path)
    validation_errors = validate_export(catalog_titles, md_dir, raw_dir)
    if validation_errors:
        print("\n  ❌ 完整性校验失败：")
        for error in validation_errors:
            print(f"     - {error}")
        print("     未生成新的全书 Markdown。")
        return False
    print(f"  ✅ 完整性校验通过: {len(catalog_titles)}/{len(catalog_titles)} 章")

    download_all_images(raw_dir, img_dir)

    total_files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    img_count = len([f for f in os.listdir(img_dir) if not f.startswith(".")])
    if not book_title: book_title = book_id
    safe = re.sub(r'[<>:"/\\|?*]', '_', book_title)
    merged = os.path.join(output_root, f"{safe}.md")
    with open(merged, "w") as out:
        out.write(f"# {book_title}\n\n**{book_author}**\n\n---\n\n")
        for fn in total_files:
            with open(os.path.join(md_dir, fn)) as chapter_file:
                out.write(chapter_file.read())
            out.write("\n\n---\n\n")
    print(f"\n{'=' * 60}")
    print(f"  ✅ 全书导出完成!  📖 {book_title} — {book_author}")
    print(f"  📄 {len(total_files)} 章, {os.path.getsize(merged):,} bytes,  🖼 {img_count} 张图")
    print(f"  📦 {merged}")
    print(f"{'=' * 60}")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导出微信读书为 Markdown")
    parser.add_argument("book_url_or_id", help="微信读书 reader URL 或 book_id")
    parser.add_argument(
        "--restart", action="store_true",
        help="从目录第一章重新导出，并将该书旧产物移动到 output/_backups/")
    args = parser.parse_args()
    raw_arg = args.book_url_or_id.strip().rstrip("/")
    book_id = raw_arg.split("/")[-1] if "weread.qq.com" in raw_arg else raw_arg
    print(f"  Book ID: {book_id}")
    success = asyncio.run(main(book_id, restart=args.restart))
    sys.exit(0 if success else 1)
