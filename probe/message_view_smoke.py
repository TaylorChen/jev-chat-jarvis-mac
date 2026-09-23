#!/usr/bin/env python3
"""probe/message_view_smoke.py — 「查看读到的消息」窗口冒烟：打开默认停在最新一条。

用户报过：打开历史消息要手工往下滑。消息是时间正序，末尾才是「现在」；这个探针断言
窗口显示后可见区域确实落在文档末尾，并把窗口渲成 PNG（/tmp/jev-message-view.png）。

Run: python -B probe/message_view_smoke.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import AppKit as A                     # noqa: E402
import Quartz                          # noqa: E402
from Foundation import NSDate          # noqa: E402

import message_view                    # noqa: E402


def pump(seconds):
    A.NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


def main():
    now = int(time.time())
    rows = [(now - (100 - i) * 60, 'them' if i % 3 else 'me', '石汀兰',
             f'第 {i} 条历史消息，用来验证打开时停在最新一条') for i in range(100)]
    viewer = message_view.MessageViewer.alloc().initWithTitle_rows_budget_('冒烟会话', rows, 100)
    A.NSApplication.sharedApplication()
    viewer.show()
    pump(0.6)
    view = viewer.text
    clip = view.enclosingScrollView().contentView()
    doc_h = view.frame().size.height
    clip_h = clip.bounds().size.height
    bottom = max(0.0, doc_h - clip_h)
    at_end = abs(clip.bounds().origin.y - bottom) < 2
    # 最后一行必须真的在可见区域里（字符偏移量在可见范围内）
    layout, container = view.layoutManager(), view.textContainer()
    glyphs = layout.glyphRangeForBoundingRect_inTextContainer_(clip.documentVisibleRect(), container)
    visible = layout.characterRangeForGlyphRange_actualGlyphRange_(glyphs, None)[0]
    last_line_start = view.textStorage().length() - len(f'第 99 条历史消息，用来验证打开时停在最新一条') - 20
    last_visible = visible.location + visible.length >= view.textStorage().length() - 2

    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
        viewer.window.windowNumber(), Quartz.kCGWindowImageBoundsIgnoreFraming)
    if image:
        rep = A.NSBitmapImageRep.alloc().initWithCGImage_(image)
        rep.representationUsingType_properties_(
            A.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(
            '/tmp/jev-message-view.png', True)

    print(f"文档 {doc_h:.0f}px · 窗口 {clip_h:.0f}px · 可见起点 {clip.bounds().origin.y:.0f}"
          f"（末尾应为 {bottom:.0f}）")
    print(f"字符范围 {visible.location}..{visible.location + visible.length}"
          f" / 共 {view.textStorage().length()} · 末行在可见区: {last_visible}")
    print('PASS: 打开即停在最新一条' if (at_end and last_visible)
          else f'FAIL: 没有停在末尾（last_line_start={last_line_start}）')
    return 0 if (at_end and last_visible) else 1


if __name__ == '__main__':
    sys.exit(main())
