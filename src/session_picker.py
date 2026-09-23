#!/usr/bin/env python3
"""session_picker.py — 「跟随哪个会话」选择窗口。

为什么需要它：微信**不记录「当前打开的会话」**。`SessionTable` 只在清未读时写时间戳，
点开一个没有未读的会话不会留下任何痕迹（实测），AX 也读不到会话列表。所以「我想盯住
这个会话」只能由用户显式指定，这个窗口就是那个入口：列出最近活跃的会话，点一行即跟随。

只读：列表数据来自 `DBReader.provider`（sqlcipher 只读打开），窗口本身不读任何文件。
"""
from __future__ import annotations

import AppKit
import objc
from Foundation import NSMakeRect, NSObject

WINDOW_W, WINDOW_H = 420, 460
PAD = 12
ROW_H = 26


class SessionPicker(NSObject):
    """单选列表：选中一行就回调 `on_pick(username)`（空串 = 自动跟随）。"""

    def initWithTitle_rows_onPick_(self, title, rows, on_pick):
        self = self.init()
        if self is None:
            return None
        self._rows = list(rows)
        self._on_pick = on_pick
        self._build_window(title)
        return self

    @objc.python_method
    def _build_window(self, title):
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_W, WINDOW_H),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered, False)
        self.window.setTitle_(title)
        # 悬浮窗是 floating level：这个窗口要能盖住它
        self.window.setLevel_(AppKit.NSFloatingWindowLevel + 1)
        self.window.setReleasedWhenClosed_(False)
        view = self.window.contentView()

        hint = AppKit.NSTextField.wrappingLabelWithString_(
            "微信不记录「当前打开的会话」：点开一个没有未读的会话不会写任何痕迹，"
            "所以自动跟随只对你清过未读的会话生效。想一直盯住某个会话，在这里点它。")
        hint.setFrame_(NSMakeRect(PAD, WINDOW_H - 66, WINDOW_W - 2 * PAD, 54))
        hint.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        hint.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        view.addSubview_(hint)

        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            NSMakeRect(PAD, PAD, WINDOW_W - 2 * PAD, WINDOW_H - 66 - 2 * PAD))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(AppKit.NSBezelBorder)
        scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)

        self.table = AppKit.NSTableView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WINDOW_W - 2 * PAD, WINDOW_H))
        column = AppKit.NSTableColumn.alloc().initWithIdentifier_("session")
        column.setWidth_(WINDOW_W - 2 * PAD - 20)
        self.table.addTableColumn_(column)
        self.table.setHeaderView_(None)
        self.table.setRowHeight_(ROW_H)
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        self.table.setAllowsEmptySelection_(True)
        scroll.setDocumentView_(self.table)
        view.addSubview_(scroll)

    @objc.python_method
    def reload(self, rows):
        self._rows = list(rows)
        self.table.reloadData()

    # ---------- 数据源 / 代理 ----------

    def numberOfRowsInTableView_(self, table):
        return len(self._rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if row < 0 or row >= len(self._rows):
            return ""
        return self._rows[row][1]

    @objc.python_method
    def pick_row(self, row):
        """（测试用）按行号触发选择。"""
        if 0 <= row < len(self._rows):
            username = self._rows[row][0]
            if self._on_pick:
                self._on_pick(username)
            self.window.orderOut_(None)
            return username
        return None

    def tableViewSelectionDidChange_(self, note):
        self.pick_row(self.table.selectedRow())

    def show(self):
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
