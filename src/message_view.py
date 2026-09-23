#!/usr/bin/env python3
"""message_view.py — 「查看读到的消息」窗口。

把悬浮窗这一跳实际读到的消息按原样列出来，用来核对「判断到底喂了什么上下文」：

  * 数据库直读：本地库里的真实历史（默认最近 100 条，和判断用的条数一致）
  * OCR 读屏：这一帧认到的那几条（含方向未确认的）

纯展示：不重新读取、不写任何东西、不联网。格式化逻辑放在模块级函数里（不依赖
AppKit），窗口只是把它画出来。

用法（hud 菜单「查看读到的消息…」）：
    viewer = MessageViewer.alloc().initWithTitle_rows_(title, rows)
    viewer.show()
    viewer.update(title, rows)      # 下一次读完后刷新已打开的窗口
"""
from __future__ import annotations

from datetime import datetime

import AppKit
import objc
from Foundation import NSMakeRect, NSMakeSize, NSRange, NSObject

WINDOW_W, WINDOW_H = 560, 620
PAD = 12


def _clock(ts: int) -> str:
    if not ts:
        return "  --:--  "
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def attributed_rows(rows):
    """rows: [(ts, who, sender, text[, img_path])] → 带内嵌图片的富文本。

    图片行（img_path 非空）渲染为：文字行 + 换行 + 缩略图附件（宽 240pt 等比）。
    """
    import time as _time
    font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(12, 0) \
        if hasattr(AppKit.NSFont, "monospacedSystemFontOfSize_weight_") \
        else AppKit.NSFont.systemFontOfSize_(12)
    attrs = {AppKit.NSFontAttributeName: font,
             AppKit.NSForegroundColorAttributeName: AppKit.NSColor.textColor}
    out = AppKit.NSMutableAttributedString.alloc().init()
    for row in rows:
        ts, who, sender, text = row[0], row[1], row[2], row[3]
        img_path = row[4] if len(row) > 4 else None
        name = "我" if who == "me" else (sender or {"them": "对方"}.get(who, "方向未确认"))
        body = text.replace("\n", " ⏎ ")
        line = f"{_clock(int(ts or 0))}  {name}: {body}\n"
        out.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(line, attrs))
        if img_path:
            img = AppKit.NSImage.alloc().initWithContentsOfFile_(str(img_path))
            if img is not None:
                size = img.size()
                if size.width > 220:
                    img.setSize_(NSMakeSize(220, size.height * 220 / size.width))
                cell = AppKit.NSTextAttachmentCell.imageCell_(img)
                att = AppKit.NSTextAttachment.alloc().init()
                att.setAttachmentCell_(cell)
                out.appendAttributedString_(
                    AppKit.NSAttributedString.alloc().initWithString_attributes_(
                        "\uFFFC\n", attrs))
    return out


def format_rows(rows) -> str:
    """rows: [(ts, who, sender, text)] → 一行一条的纯文本（时间 说话人: 正文）。"""
    out = []
    for ts, who, sender, text in rows:
        name = "我" if who == "me" else (sender or {"them": "对方"}.get(who, "方向未确认"))
        body = text.replace("\n", " ⏎ ")
        out.append(f"{_clock(int(ts or 0))}  {name}: {body}")
    return "\n".join(out)


def format_summary(title: str, rows, budget: int) -> str:
    """窗口顶部的一行说明：会话、条数、判断实际用了几条。"""
    n = len(rows)
    used = budget if budget else n
    return (f"会话：{title or '（未识别）'} · 读到 {n} 条 · "
            f"判断/生成用最近 {min(used, max(n - 1, 0))} 条（当前消息除外）")


class MessageViewer(NSObject):
    """只读滚动窗口；复用同一个实例反复打开，避免每次点菜单都新建窗口。"""

    def initWithTitle_rows_budget_(self, title, rows, budget):
        self = self.init()
        if self is None:
            return None
        self._build()
        self.reload(title, rows, budget)
        return self

    def _build(self):
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_W, WINDOW_H),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskResizable,
            AppKit.NSBackingStoreBuffered, False)
        self.window.setTitle_("读到的消息")
        # 悬浮窗是 floating level：这个窗口要能盖住它，否则点开却看不见
        self.window.setLevel_(AppKit.NSFloatingWindowLevel + 1)
        self.window.setReleasedWhenClosed_(False)
        view = self.window.contentView()

        self.summary = AppKit.NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD, WINDOW_H - 34, WINDOW_W - 2 * PAD, 22))
        self.summary.setFont_(AppKit.NSFont.boldSystemFontOfSize_(12))
        self.summary.setEditable_(False)
        self.summary.setBordered_(False)
        self.summary.setDrawsBackground_(False)
        view.addSubview_(self.summary)

        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            NSMakeRect(PAD, PAD, WINDOW_W - 2 * PAD, WINDOW_H - 34 - 2 * PAD))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(AppKit.NSBezelBorder)
        scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        self.text = AppKit.NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WINDOW_W - 2 * PAD, WINDOW_H - 34 - 2 * PAD))
        self.text.setEditable_(False)
        self.text.setRichText_(True)   # 允许内嵌图片附件
        self.text.setFont_(AppKit.NSFont.fontWithName_size_("Menlo", 12)
                           or AppKit.NSFont.systemFontOfSize_(12))
        self.text.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        self.text.setTextContainerInset_(NSMakeSize(6, 6))
        # 纵向可伸缩 + 容器高度不跟随视图高度：否则文本视图不会随内容长高，滚动区永远
        # 只有窗口那么高，「滚到末尾」就只能滚到当时的底部（实测停在 183/1424）
        self.text.setVerticallyResizable_(True)
        self.text.setHorizontallyResizable_(False)
        self.text.setMaxSize_(NSMakeSize(1e7, 1e7))
        container = self.text.textContainer()
        container.setWidthTracksTextView_(True)
        container.setHeightTracksTextView_(False)
        container.setContainerSize_(NSMakeSize(WINDOW_W - 2 * PAD - 12, 1e7))
        scroll.setDocumentView_(self.text)
        view.addSubview_(scroll)

    @objc.python_method
    def reload(self, title, rows, budget):
        """换一批内容重画（名字不叫 update：那会撞上 NSObject 的同名选择器）。"""
        self.summary.setStringValue_(format_summary(title, rows, budget))
        self.text.textStorage().setAttributedString_(attributed_rows(rows))
        self._scroll_to_newest()
        self._defer_scroll_to_newest()

    @objc.python_method
    def _defer_scroll_to_newest(self):
        """再滚一次（下一轮 runloop）。

        文本视图的内容高度是布局完成后才长开的：立刻滚只能滚到「当时的底部」，
        实测停在文档中段。窗口关闭时定时器会自然失效，不留下副作用。
        """
        if getattr(self, "_scroll_timer", None) is not None:
            return
        self._scroll_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, "scrollToNewest:", None, False)

    def scrollToNewest_(self, _timer):
        self._scroll_timer = None
        self._scroll_to_newest()

    @objc.python_method
    def _scroll_to_newest(self):
        """默认视线停在**最新一条**：消息是时间正序，末尾才是「现在」。

        以前这里滚到 (0,0)（= 最早那条），打开窗口得手动往下滑到底，用户报的就是这个。
        先强制排版再滚：`scrollToEndOfDocument_` 在布局还没长开时只会滚到当时的「底部」，
        实测停在文档中段（183/1424）。
        """
        end = self.text.textStorage().length()
        self.text.setSelectedRange_(NSRange(end, 0))
        layout = self.text.layoutManager()
        container = self.text.textContainer()
        if layout is not None and container is not None:
            layout.ensureLayoutForTextContainer_(container)
            used = layout.usedRectForTextContainer_(container)
            self.text.setFrameSize_(NSMakeSize(self.text.frame().size.width,
                                               max(self.text.frame().size.height,
                                                   used.size.height + 12)))
        scroll = self.text.enclosingScrollView()
        if scroll is None:
            self.text.scrollToEndOfDocument_(None)
            return
        clip = scroll.contentView()
        offset = max(0.0, self.text.frame().size.height - clip.bounds().size.height)
        clip.scrollToPoint_((0.0, offset))
        scroll.reflectScrolledClipView_(clip)

    def show(self):
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        # 显示之后再来一次：文本布局这时才确定，只在那之前滚可能停在半路
        self._scroll_to_newest()
        self._defer_scroll_to_newest()
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
