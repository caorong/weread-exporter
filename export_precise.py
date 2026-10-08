#!/usr/bin/env python3
"""
微信读书导出 — 精确图文版 v3

纵向捕获：按各 Canvas 和 DOM 的文档坐标排列文字与图片，
确认目录标题起点，滚动至下一节或当前内容末尾后保存，支持断点续传。
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
    window.__wr_canvas_chars = new WeakMap();
    for (const dimension of ['width', 'height']) {
        const descriptor = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, dimension);
        Object.defineProperty(HTMLCanvasElement.prototype, dimension, {
            ...descriptor, set(value) {
                window.__wr_canvas_chars.delete(this);
                descriptor.set.call(this, value);
            }
        });
    }
    var origClear = CanvasRenderingContext2D.prototype.clearRect;
    CanvasRenderingContext2D.prototype.clearRect = function(x, y, w, h) {
        const chars = window.__wr_canvas_chars.get(this.canvas);
        if (chars) {
            const transform = this.getTransform();
            const a = transform.transformPoint(new DOMPoint(x, y));
            const b = transform.transformPoint(new DOMPoint(x + w, y + h));
            for (const [key, c] of chars) {
                if (c.x >= Math.min(a.x, b.x) && c.x <= Math.max(a.x, b.x) &&
                    c.y >= Math.min(a.y, b.y) && c.y <= Math.max(a.y, b.y)) chars.delete(key);
            }
        }
        return origClear.apply(this, arguments);
    };
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && text.trim()) {
            window.__wr_chars.push({t: text, x: Math.round(x*10)/10, y: Math.round(y*10)/10});
            let chars = window.__wr_canvas_chars.get(this.canvas);
            if (!chars) {
                chars = new Map();
                window.__wr_canvas_chars.set(this.canvas, chars);
            }
            const transform = this.getTransform();
            const metrics = this.measureText(text);
            const top = y - (metrics.fontBoundingBoxAscent || 0);
            const point = transform.transformPoint(new DOMPoint(x, top));
            chars.set(x + ',' + y, {t: text, x: point.x, y: point.y});
        }
        return origFill.apply(this, arguments);
    };
    // The reader also copies an offscreen text canvas into its displayed canvas.
    var origDraw = CanvasRenderingContext2D.prototype.drawImage;
    CanvasRenderingContext2D.prototype.drawImage = function(source, ...args) {
        const drawn = window.__wr_canvas_chars.get(source);
        if (drawn) {
            let sx = 0, sy = 0, sw = source.width, sh = source.height;
            let dx, dy, dw, dh;
            if (args.length === 2) [dx, dy, dw, dh] = [...args, sw, sh];
            else if (args.length === 4) [dx, dy, dw, dh] = args;
            else [sx, sy, sw, sh, dx, dy, dw, dh] = args;
            let dest = window.__wr_canvas_chars.get(this.canvas);
            if (!dest) {
                dest = new Map();
                window.__wr_canvas_chars.set(this.canvas, dest);
            }
            const transform = this.getTransform();
            for (const c of Array.from(drawn.values())) {
                if (c.x < sx || c.x >= sx + sw || c.y < sy || c.y >= sy + sh) continue;
                const point = transform.transformPoint(new DOMPoint(
                    dx + (c.x - sx) * dw / sw, dy + (c.y - sy) * dh / sh));
                dest.set(point.x + ',' + point.y, {t: c.t, x: point.x, y: point.y});
            }
        }
        return origDraw.call(this, source, ...args);
    };
    window.__wr_reset = function() { window.__wr_chars = []; };
    window.__wr_count = function() { return window.__wr_chars.length; };
})();
"""

# Project each canvas independently into document coordinates. A vertical reader
# may have several tall canvases plus a virtualized DOM character layer.
DOCUMENT_SNAPSHOT_JS = """
() => {
    const chars = [], images = [];
    let untrackedCanvases = 0;
    const root = document.querySelector('.renderTargetContent');
    if (!root) return null;
    for (const canvas of document.querySelectorAll('canvas')) {
        const rect = canvas.getBoundingClientRect();
        if (!rect.width || !rect.height || !canvas.width || !canvas.height) continue;
        const drawn = window.__wr_canvas_chars?.get(canvas);
        if (!drawn) { untrackedCanvases++; continue; }
        for (const c of drawn.values()) chars.push({t: c.t,
            x: rect.left + c.x * rect.width / canvas.width,
            y: rect.top + scrollY + c.y * rect.height / canvas.height});
    }
    for (const el of root.querySelectorAll('.wr_absolute')) {
        const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
        if (!rect.width || !rect.height || style.display === 'none' ||
                style.visibility === 'hidden') continue;
        if (el.tagName === 'IMG') {
            const src = el.src || el.getAttribute('data-src') || '';
            if (src.includes('res.weread.qq.com/wrepub') && rect.width > 40 && rect.height > 40)
                images.push({src, top: rect.top + scrollY,
                    w: el.naturalWidth || el.width, h: el.naturalHeight || el.height});
        } else if (el.textContent.trim()) {
            chars.push({t: el.textContent, x: rect.left, y: rect.top + scrollY});
        }
    }
    return {chars, images, untrackedCanvases, y: scrollY, height: innerHeight,
        maxY: Math.max(0, document.documentElement.scrollHeight - innerHeight)};
}
"""

MEASURE_RE = re.compile(r'^[a-zA-Z0-9`~!@#$%^&*()\-_=+\[\]{}|;:\',<.>/?\\"\s]+$')
SENTENCE_END = set("。！？；：」）】》…—")


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
            out.append(f"![图](../images/{fname})")
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
    # EPUB renderers insert zero-width break markers around punctuation.
    value = re.sub(r"[\s\u200b\u2060\ufeff\u00ad]+", "", title or "")
    # Printed division titles may decorate the leading label: ｜上编｜动态篇.
    return re.sub(r"^[|｜]([^|｜]+)[|｜]", r"\1", value)


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


def heading_span(blocks, title, start=0):
    """Match an entire heading, including consecutive wrapped title lines."""
    key = canonical_title(title)
    for i in range(start, len(blocks)):
        value = ""
        for j in range(i, min(i + 6, len(blocks))):
            if blocks[j].get("type") != "text":
                break
            part = canonical_title(blocks[j].get("text", ""))
            # Printed headings may replace a catalog colon with a line break:
            # "附录：方法索引" -> ["附录", "方法索引"]. Only restore a
            # separator at a real block boundary, keeping full-title matching.
            if j > i and value and part and not key.startswith(value + part):
                for separator in ("：", ":"):
                    if key.startswith(value + separator + part):
                        value += separator
                        break
            value += part
            if value == key:
                return i, j + 1
            if not key.startswith(value):
                break
    return None


def partition_catalog_sections(blocks, current_index, catalog_titles):
    """Split only at adjacent catalog headings; never jump to a distant namesake."""
    if not 0 <= current_index < len(catalog_titles):
        raise ExportError(f"当前目录下标无效: {current_index}")
    sections, complete = {}, {}
    cursor = 0
    for index in range(current_index, len(catalog_titles)):
        heading = heading_span(blocks, catalog_titles[index], cursor)
        body_start = heading[1] if heading else cursor
        next_heading = (heading_span(blocks, catalog_titles[index + 1], body_start)
                        if index + 1 < len(catalog_titles) else None)
        end = next_heading[0] if next_heading else len(blocks)
        prefix = blocks[cursor:heading[0]] if heading else []
        # A title page may have a cover above its heading. Do not keep preceding
        # prose from another section when locating an internal chapter anchor.
        images = prefix if prefix and all(b.get("type") == "img" for b in prefix) else []
        sections[index] = images + list(blocks[body_start:end])
        complete[index] = next_heading is not None
        if next_heading is None:
            break
        cursor = next_heading[0]
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


def merge_positioned_blocks(existing, incoming):
    """Combine document snapshots without appending repeated render windows."""
    result = list(existing)
    for block in incoming:
        signature = (block["type"], block.get("text", block.get("src")))
        duplicate = next((i for i, old in enumerate(result)
                          if (old["type"], old.get("text", old.get("src"))) == signature
                          and (block["type"] == "img" or
                               abs(old["y"] - block["y"]) <= 6)), None)
        if duplicate is None:
            result.append(block)
        else:
            result[duplicate] = block
    return sorted(result, key=lambda block: block["y"])


async def capture_catalog_section(page, current_index, catalog_titles, start_timeout=10):
    """Read one verified section, including images below the initial viewport."""
    blocks = []
    initial_y = await page.evaluate("() => window.scrollY")
    title = catalog_titles[current_index]
    snapshots = 0
    load_deadline = asyncio.get_running_loop().time() + start_timeout
    try:
        for _ in range(200):
            snapshot = await page.evaluate(DOCUMENT_SNAPSHOT_JS)
            if snapshot is None:
                raise ExportError("正文容器不存在，无法提取章节")
            incoming = [{"type": "text", "text": line["text"], "y": line["y"]}
                        for line in chars_to_lines(snapshot["chars"])]
            incoming += [{"type": "img", "src": im["src"], "w": im["w"],
                          "h": im["h"], "y": im["top"]} for im in snapshot["images"]]
            blocks = merge_positioned_blocks(blocks, incoming)
            snapshots += 1

            candidates = []
            cursor = 0
            while (match := heading_span(blocks, title, cursor)) is not None:
                candidates.append(match)
                cursor = match[1]
            heading = min(candidates, key=lambda span:
                          abs(blocks[span[0]]["y"] - initial_y - 100)) if candidates else None
            if heading is None:
                # A separate illustration/cover page may have no printed title.
                if (blocks and all(b["type"] == "img" for b in blocks) and
                        not snapshot.get("untrackedCanvases", 0) and
                        await _selected_catalog_index(page) == current_index):
                    heading = (0, 0)
                else:
                    if asyncio.get_running_loop().time() < load_deadline:
                        blocks = []
                        await asyncio.sleep(.5)
                        continue
                    raise ExportError(f"未在正文中确认章节起点「{title}」，拒绝保存未定位内容")
            next_heading = (heading_span(blocks, catalog_titles[current_index + 1], heading[1])
                            if current_index + 1 < len(catalog_titles) else None)
            at_bottom = snapshot["y"] >= snapshot["maxY"] - 2
            reached_next = (next_heading is not None and
                            blocks[next_heading[0]]["y"] <= snapshot["y"] + snapshot["height"])
            if at_bottom or reached_next:
                end = next_heading[0] if next_heading else len(blocks)
                prefix = blocks[:heading[0]]
                # Preserve cover artwork only when there is no preceding prose.
                prefix = prefix if all(b["type"] == "img" for b in prefix) else []
                content = prefix + blocks[heading[1]:end]
                return content, snapshots, not content

            next_y = min(snapshot["maxY"], snapshot["y"] + max(400, snapshot["height"] * .75))
            await page.evaluate("y => window.scrollTo(0, y)", next_y)
            await asyncio.sleep(.35)
            await wait_stable(page)
        raise ExportError(f"章节「{title}」扫描超出限制，未确认结束位置")
    finally:
        await page.evaluate("y => window.scrollTo(0, y)", initial_y)
        await asyncio.sleep(.3)


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


async def _catalog_index_matches(page, expected_index, catalog_titles):
    """Accept a bottom-of-page heading when the catalog highlight lags behind."""
    current_index = await _selected_catalog_index(page)
    if current_index == expected_index:
        return True
    if not 0 <= current_index < expected_index < len(catalog_titles):
        return False
    title_key = canonical_title(catalog_titles[expected_index])
    if not title_key or sum(
            canonical_title(title) == title_key for title in catalog_titles) != 1:
        return False

    chars = await page.evaluate("""() => {
        const maxY = Math.max(0, document.documentElement.scrollHeight - innerHeight);
        if (window.scrollY < maxY - 2) return [];
        return Array.from(document.querySelectorAll('.renderTargetContent .wr_absolute'))
            .map(el => {
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return {t: el.textContent || '', x: r.left, y: r.top,
                    visible: r.width > 0 && r.height > 0 && r.top >= 0 &&
                        r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth &&
                        style.display !== 'none' && style.visibility !== 'hidden'};
            }).filter(c => c.t.trim());
    }""")
    # Reconstruct whole lines: the reader usually stores one DOM node per glyph.
    # Reject partially clipped lines instead of matching a visible title prefix.
    hidden_rows = {round(c["y"] / 3) * 3 for c in chars if not c["visible"]}
    lines = [{"type": "text", **line} for line in chars_to_lines(chars)]
    match = heading_span(lines, catalog_titles[expected_index])
    return (match is not None and
            heading_span(lines, catalog_titles[expected_index], match[1]) is None and
            all(line["y"] not in hidden_rows for line in lines[match[0]:match[1]]))


async def _open_catalog(page):
    if not await _catalog_is_open(page):
        await page.click("button.readerControls_item.catalog", timeout=5000)
    await page.locator(".readerCatalog_list_item:visible").first.wait_for(
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
        if await _catalog_index_matches(page, expected_index, catalog_titles):
            return expected_index
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


async def capture_verified_section(page, current_index, catalog_titles):
    try:
        return await capture_catalog_section(page, current_index, catalog_titles)
    except ExportError:
        # Rebuild the render state once; never save a failed capture as empty.
        print(f"  ↻ 重新加载并核对「{catalog_titles[current_index]}」", flush=True)
        await page.reload(wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        recovered_index, recovered_titles = await goto_catalog_chapter(
            page, resume_after_index=current_index - 1 if current_index else None)
        if recovered_index != current_index or recovered_titles != catalog_titles:
            raise ExportError("重新加载后目录发生变化，已停止导出")
        return await capture_catalog_section(page, current_index, catalog_titles)


def save_chapter(ch_title, blocks, ch_idx, md_dir, raw_dir,
                 catalog_index=None, title_only=False):
    if not ch_title.strip():
        raise ExportError("拒绝保存标题为空的章节")
    if title_only and blocks:
        raise ExportError("仅标题章节不应包含正文块")
    body, img_records = render_chapter_md(ch_title, blocks, ch_idx)
    text_len = sum(len(b["text"]) for b in blocks if b["type"] == "text")
    with open(os.path.join(md_dir, f"{ch_idx:04d}.md"), "w") as f:
        f.write(body)
    record = {"title": ch_title, "images": img_records, "text_len": text_len,
              "capture_version": 2}
    if title_only:
        record["title_only_verified"] = True
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
        if (not record.get("text_len", 0) and not record.get("images", []) and
                not (record.get("capture_version") == 2 and
                     record.get("title_only_verified") is True)):
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


async def run_session(book_id, md_dir, raw_dir, start_idx,
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
                            wait_until="domcontentloaded", timeout=30000)
            await page.locator(
                "button.readerControls_item.isNormalReader, "
                "button.readerControls_item.isHorizontalReader").first.wait_for(
                    state="visible", timeout=30000)
            await asyncio.sleep(2)

            await ensure_vertical_reading_mode(page, headless=headless)
            book_title, book_author = await fetch_book_title(page)
            if catalog_path:
                with open(os.path.join(os.path.dirname(catalog_path), "_book.json"), "w") as f:
                    json.dump({"title": book_title, "author": book_author}, f, ensure_ascii=False)
            target_index, catalog_titles = await goto_catalog_chapter(
                page, resume_after_title=resume_after_title,
                resume_after_index=resume_after_index,
                catalog_path=catalog_path)
            if target_index is None:
                return book_title, book_author, 0, 0, start_idx, True

            current_index = await _wait_for_catalog_index(
                page, target_index, catalog_titles)
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

                ch_blocks, page_num, title_only = await capture_verified_section(
                    page, current_index, catalog_titles)
                n, imgs = save_chapter(
                    current_chapter, ch_blocks, ch_idx, md_dir, raw_dir,
                    catalog_index=current_index, title_only=title_only)
                total_chars += n
                note = f" +{len(imgs)}图" if imgs else ""
                end_note = (
                    " [全书末尾]"
                    if current_index == len(catalog_titles) - 1 else "")
                print(f"  [{ch_idx:4d}] {current_chapter[:32]:32s} "
                      f"{n:6d}字 ({page_num}次采样){note}{end_note}")
                chapters_this_session += 1
                ch_idx += 1

                if current_index == len(catalog_titles) - 1:
                    reached_end = True
                    break

                next_index = current_index + 1
                ch_blocks = []
                await _click_catalog_index(page, next_index, catalog_titles)
                current_index = await _wait_for_catalog_index(
                    page, next_index, catalog_titles)
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

    catalog_path = os.path.join(book_dir, "_catalog.json")
    book_title = book_author = ""
    export_complete = False
    try:
        with open(os.path.join(book_dir, "_book.json")) as f:
            metadata = json.load(f)
        catalog_titles = load_catalog_titles(catalog_path)
        current_capture = True
        for filename in os.listdir(raw_dir):
            if filename.endswith(".json"):
                with open(os.path.join(raw_dir, filename)) as f:
                    current_capture &= json.load(f).get("capture_version") == 2
        if (metadata.get("title") and catalog_titles and current_capture and
                not validate_export(catalog_titles, md_dir, raw_dir)):
            book_title, book_author = metadata["title"], metadata.get("author", "")
            export_complete = True
            print("  ✅ 已有完整章节，直接校验图片并合并，无需重开浏览器。")
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    session = 0
    no_progress_sessions = 0
    while not export_complete:
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
                book_id, md_dir, raw_dir, start_idx,
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
                content = chapter_file.read()
                content = content.replace("](../images/", f"]({book_id}/images/")
                content = content.replace("](images/", f"]({book_id}/images/")
                out.write(content)
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
