#!/usr/bin/env python3
"""probe/history_ui_smoke.py — 历史会话窗口冒烟：初始化 + 选中会话 + 内容加载计时

默认跑 live 模式（JEV_LIVE_DB=1，直读微信加密库）；想跑解密快照就把这一行
去掉。会短暂打开一个原生窗口，6 秒后自动退出。

Run: python -B probe/history_ui_smoke.py   （仓库根目录）
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
os.environ["JEV_LIVE_DB"] = "1"

from AppKit import NSApplication, NSApplicationActivationPolicyRegular, NSTimer  # noqa: E402
from Foundation import NSObject  # noqa: E402
from PyObjCTools import AppHelper  # noqa: E402

from history_ui import Controller  # noqa: E402


class Probe(NSObject):
    def tick_(self, timer):
        n = timer.userInfo()
        txt = self.ctl.text.string() or ""
        if n == 1:
            print(f"[{time.time()-self.t0:.1f}s] 初始化: {self.ctl.table.numberOfRows()} 行 | "
                  f"状态: {self.ctl.status.stringValue()}")
            self.ctl.table.selectRowIndexes_byExtendingSelection_(
                __import__('AppKit').NSIndexSet.indexSetWithIndex_(1), False)
            self.ctl.tableViewSelectionDidChange_(None)
            self.t1 = time.time()
        elif n == 2:
            print(f"[{time.time()-self.t0:.1f}s] 选中会话加载中… 右侧 {len(txt)} 字符")
        elif n == 3:
            lines = txt.splitlines()
            print(f"[{time.time()-self.t0:.1f}s] 右侧首行: {lines[0] if lines else '(空)'}")
            print(f"    字符数: {len(txt)} | 状态: {self.ctl.status.stringValue()}")
            view = self.ctl.text
            clip = view.enclosingScrollView().contentView()
            doc_h, clip_h = view.frame().size.height, clip.bounds().size.height
            print(f"    文档 {doc_h:.0f}px / 窗口 {clip_h:.0f}px · 可见起点 {clip.bounds().origin.y:.0f}"
                  f"（末尾 {max(0.0, doc_h - clip_h):.0f}）→ "
                  f"{'停在最新一条' if abs(clip.bounds().origin.y - max(0.0, doc_h - clip_h)) < 2 else '★没停在末尾'}")
            AppHelper.stopEventLoop()


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    ctl = Controller.alloc().initWithUI_(None)
    ctl.win.setFrameAutosaveName_("HistoryAnalyzeWindow")
    ctl.win.center()
    ctl.win.makeKeyAndOrderFront_(None)

    probe = Probe.alloc().init()
    probe.ctl = ctl
    probe.t0 = time.time()
    for i in (1, 2, 3):
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0 * i, probe, 'tick:', i, False)
    AppHelper.runConsoleEventLoop()


if __name__ == "__main__":
    main()
