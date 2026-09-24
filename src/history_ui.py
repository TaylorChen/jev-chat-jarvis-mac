#!/usr/bin/env python3
"""history_ui.py — 历史会话分析的原生窗口（独立于悬浮窗，直接可跑）

左侧：分类（全部/个人/群聊/公众号）+ 会话列表；右侧：分析结果。
三个动作：总结（LLM 议题/结论/承诺待办/风险）、意图分布（本地抽样判断）、
候选回复（对方未回复消息 → 话术生成）。
数据源：wcdb-key-tool 解密库（src/history.py），凭据与生成层共用一套。

用法：
  uv run python src/history_ui.py
环境变量：
  JEV_LIVE_DB=1  直读微信 live 加密库（实时数据）；默认读解密快照
  JEV_DB_DIR     解密快照目录（默认见 src/wechat_keys.py）
隐私：intent 全本地；summary/reply 会把对话文本发给生成层配置的 LLM。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSBezelStyleRounded, NSButton, NSColor,
                    NSFont, NSIndexSet, NSMakeRect, NSSegmentedControl, NSScrollView,
                    NSTableColumn, NSTableView, NSTextView, NSTextField, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled, NSViewWidthSizable, NSViewHeightSizable,
                    NSObject)
from Foundation import NSMakePoint, NSMakeSize, NSRange
from PyObjCTools import AppHelper

sys_path = str(Path(__file__).parent)
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from history import HistoryProvider  # noqa: E402
import wechat_keys  # noqa: E402

DEFAULT_DB_DIR = wechat_keys.decrypted_dir()
WINDOW_W, WINDOW_H = 900, 600
CATEGORIES = ("全部", "个人", "群聊", "公众号")
TAG_REFRESH, TAG_SUMMARY, TAG_INTENT, TAG_REPLY = 0, 1, 2, 3


def _gray(a: float = 1.0):
    return NSColor.colorWithCalibratedWhite_alpha_(0.45, a)


def _status(ctl, msg: str):
    ctl.status.setStringValue_(msg)


def _result(ctl, text: str, to_end: bool = False):
    """把文本放进右侧面板。to_end=True 用于会话内容（时间正序，末尾才是最新一条）；
    分析结果保持从顶部开始读（总结的第一行就是标题）。"""
    ctl.text.setString_(text)
    if to_end:
        end = ctl.text.textStorage().length()
        ctl.text.setSelectedRange_(NSRange(end, 0))
        ctl.text.scrollToEndOfDocument_(None)
    else:
        ctl.text.setSelectedRange_(NSRange(0, 0))
        ctl.text.scrollRangeToVisible_(NSRange(0, 0))


def _busy(ctl, flag: bool):
    ctl.busy = flag
    for b in (ctl.btn_summary, ctl.btn_intent, ctl.btn_reply, ctl.btn_refresh):
        b.setEnabled_(not flag)


def _finish(ctl, text: str, status: str):
    def apply():
        _result(ctl, text)
        _status(ctl, status)
        _busy(ctl, False)
    AppHelper.callAfter(apply)


# ---------- 三个分析任务（工作线程；只经 _finish/_status 回主线程） ----------

def _run_summary(ctl, username: str, display: str):
    try:
        from generate import Generator
        msgs = ctl.provider.messages(username, limit=200)
        if not msgs:
            _finish(ctl, "该会话没有文本消息（图片/语音/转账等非文本无法分析）。", "无数据")
            return
        tr = "\n".join(
            f"{m['who']}({m['name']}) {datetime.fromtimestamp(m['ts']).strftime('%m-%d %H:%M')}: {m['text']}"
            for m in msgs)
        if len(tr) > 14000:
            tr = "……（更早内容已截断）\n" + tr[-14000:]
        AppHelper.callAfter(lambda: _status(ctl, "LLM 总结中…"))
        out = Generator()._call(
            "你是聊天记录分析助手。以下是某人与我的最近对话（时间正序，已标注 我/对方）：\n\n"
            f"{tr}\n\n请用 Markdown 输出：\n## 议题概览（3-5 条）\n## 已达成结论/决定\n"
            "## 承诺与待办（谁欠谁什么，尽量引用原话）\n## 风险或需要留意的话\n## 建议的下一步\n")
        _finish(ctl, out or "(LLM 返回为空)",
                f"总结完成 — {display}，{len(msgs)} 条文本消息，{len(out or '')} 字")
    except Exception as e:
        _finish(ctl, f"总结失败: {type(e).__name__}: {e}", "⚠ 失败")


def _run_intent(ctl, username: str, display: str):
    try:
        from judge import make_judge
        msgs = ctl.provider.messages(username, limit=2000)
        theirs = [m for m in msgs if m["who"] == "对方"]
        if not theirs:
            _finish(ctl, f"该会话共 {len(msgs)} 条消息，其中对方文本消息 0 条\n"
                         f"（其余为图片/语音/系统消息，或均为自己发送，无法做意图分析）。", "无数据")
            return
        step = max(1, len(theirs) // 50)
        sample = theirs[::step][:50]
        judge = make_judge()
        cloud = type(judge).__name__ == "FallbackJudge"
        AppHelper.callAfter(lambda: _status(ctl,
            f"{"Jev 云端" if cloud else "本地模型"}判断中 0/{len(sample)}…"))
        judge.warm()
        dist: dict[str, int] = {}
        risks = []
        for i, m in enumerate(sample, 1):
            v = judge.judge(m["text"])
            dist[v["intent"]] = dist.get(v["intent"], 0) + 1
            risks.append((v["risk"], m))
            if i % 5 == 0 or i == len(sample):
                AppHelper.callAfter(
                    lambda i=i, n=len(sample): _status(
                        ctl, f"{"Jev 云端" if cloud else "本地模型"}判断中 {i}/{n}…"))
        n = len(sample)
        mode = "Jev 云端，消息已发送到配置服务" if cloud else "本地推理，未出网"
        out = [f"# 意图分布 — {display} → 我（抽样 {n} 条，{mode}）", ""]
        for intent, c in sorted(dist.items(), key=lambda kv: -kv[1]):
            bar = "█" * round(c / n * 30)
            out.append(f"{intent:<12} {c:>4}  {c / n * 100:5.1f}%  {bar}")
        out += ["", "风险最高的 5 条："]
        for risk, m in sorted(risks, key=lambda kv: -kv[0])[:5]:
            when = datetime.fromtimestamp(m["ts"]).strftime("%m-%d %H:%M")
            out.append(f"  风险 {risk:>4}  {when}  「{m['text'][:36]}…」")
        _finish(ctl, "\n".join(out), f"意图分析完成 — {display}")
    except Exception as e:
        _finish(ctl, f"意图分析失败: {type(e).__name__}: {e}\n"
                     f"（本地模型首次使用会下载约 7GB；或配置 TYPESAFE_API_KEY 走云端）", "⚠ 失败")


def _run_reply(ctl, username: str, display: str):
    try:
        from generate import Generator
        from judge import make_judge
        msgs = ctl.provider.messages(username, limit=500)
        last_idx, last_their = None, None
        for i, m in enumerate(msgs):
            if m["who"] == "对方":
                last_idx, last_their = i, m
        if not last_their:
            _finish(ctl, "没找到对方的消息。", "无数据")
            return
        pending = all(m["who"] != "我" for m in msgs[last_idx + 1:])
        when = datetime.fromtimestamp(last_their["ts"]).strftime("%m-%d %H:%M")
        AppHelper.callAfter(lambda: _status(ctl, "本地判断 + 生成候选中…"))
        intent = ""
        try:
            intent = make_judge().judge(last_their["text"])["intent"]
        except Exception:
            pass
        context = "\n".join(f"{m['who']}({m['name']}): {m['text']}" for m in msgs[-10:])
        gen = Generator().generate(last_their["text"], intent, None, context)
        out = [f"# 候选回复 — {display}", "",
               f"对方最后一条消息：{when}（{'尚未回复' if pending else '之后你已回复'}）"]
        if intent:
            out.append(f"本地判断：意图 {intent}")
        out.append("")
        for grp in gen.get("groups", []):
            out.append(f"【{grp.get('tone', '?')}】")
            out += [f"  · {t}" for t in grp.get("texts", [])]
            out.append("")
        if gen.get("error"):
            out.append(f"⚠ {gen['error']}")
        _finish(ctl, "\n".join(out), f"候选回复完成 — {display}")
    except Exception as e:
        _finish(ctl, f"生成失败: {type(e).__name__}: {e}", "⚠ 失败")


RUNNERS = {TAG_SUMMARY: _run_summary, TAG_INTENT: _run_intent, TAG_REPLY: _run_reply}


class Controller(NSObject):
    provider = None
    convs: list = []        # 全量
    visible: list = []      # 分类过滤后
    category = "全部"
    selected_username: str | None = None
    busy = False

    def initWithUI_(self, db_dir):
        self = objc.super(Controller, self).init()
        if self is None:
            return None
        import os as _os
        if _os.environ.get("JEV_LIVE_DB") == "1":
            from live_db import LiveProvider
            self.provider = LiveProvider(_os.environ.get("JEV_LIVE_DIR") or None)
        else:
            self.provider = HistoryProvider(db_dir or None)
        self._live = _os.environ.get("JEV_LIVE_DB") == "1"
        self.busy = False
        self._build_window()
        self.refresh_(None)
        return self

    def _build_window(self):
        w, h = WINDOW_W, WINDOW_H
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
        win.setTitle_("历史会话分析 — jev-chat-jarvis")
        win.setContentMinSize_(NSMakeSize(760, 480))
        cv = win.contentView()
        self.win = win

        # 分类
        seg = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(20, h - 44, 300, 26))
        seg.setSegmentCount_(len(CATEGORIES))
        for i, label in enumerate(CATEGORIES):
            seg.setLabel_forSegment_(label, i)
            seg.setWidth_forSegment_(70 if label != "公众号" else 84, i)
        seg.setSelected_forSegment_(True, 0)
        seg.setTarget_(self)
        seg.setAction_("onSegment:")
        cv.addSubview_(seg)
        self.seg = seg

        # 动作按钮（右侧）
        self.buttons = {}
        for x, bw, title, tag in ((w - 60, 40, "⟳", TAG_REFRESH),
                                  (w - 416, 100, "总结", TAG_SUMMARY),
                                  (w - 308, 108, "意图分布", TAG_INTENT),
                                  (w - 192, 118, "候选回复", TAG_REPLY)):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(x, h - 48, bw, 28))
            b.setTitle_(title)
            b.setBezelStyle_(NSBezelStyleRounded)
            b.setTag_(tag)
            b.setTarget_(self)
            b.setAction_("onAction:")
            cv.addSubview_(b)
            self.buttons[tag] = b
        self.btn_refresh = self.buttons[TAG_REFRESH]
        self.btn_summary = self.buttons[TAG_SUMMARY]
        self.btn_intent = self.buttons[TAG_INTENT]
        self.btn_reply = self.buttons[TAG_REPLY]

        # 左：会话表格
        self.table = NSTableView.alloc().init()
        col = NSTableColumn.alloc().initWithIdentifier_("conv")
        col.setTitle_("会话")
        col.setWidth_(280)
        col.setResizingMask_(16)   # NSTableColumnAutoresizingMask
        self.table.addTableColumn_(col)
        self.table.setDelegate_(self)
        self.table.setDataSource_(self)
        self.table.setRowHeight_(26)
        self.table.setAllowsMultipleSelection_(False)
        left_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 44, 300, h - 104))
        left_scroll.setHasVerticalScroller_(True)
        left_scroll.setDocumentView_(self.table)
        left_scroll.setAutohidesScrollers_(True)
        self.left_scroll = left_scroll
        cv.addSubview_(left_scroll)

        # 右：结果
        self.text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w - 396, h - 130))
        self.text.setEditable_(False)
        self.text.setRichText_(False)
        self.text.setFont_(NSFont.systemFontOfSize_(13))
        self.text.setTextContainerInset_((10.0, 10.0))
        right_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(332, 44, w - 352, h - 104))
        right_scroll.setHasVerticalScroller_(True)
        right_scroll.setDocumentView_(self.text)
        right_scroll.setAutohidesScrollers_(True)
        self.right_scroll = right_scroll
        cv.addSubview_(right_scroll)

        # 底部状态
        self.status = NSTextField.labelWithString_("就绪")
        self.status.setFont_(NSFont.systemFontOfSize_(11))
        self.status.setTextColor_(_gray())
        self.status.setFrame_(NSMakeRect(22, 14, w - 44, 20))
        cv.addSubview_(self.status)

        # 任意窗口尺寸下都保持布局正确（autosave 恢复的旧尺寸也能适配）
        win.setDelegate_(self)
        self._layout()

    def _layout(self):
        """按当前内容区尺寸重摆所有控件：顶部工具条贴顶，左右栏自适应，不依赖设计尺寸。"""
        b = self.win.contentView().bounds()
        w, h = b.size.width, b.size.height
        self.seg.setFrame_(NSMakeRect(20, h - 44, 300, 26))
        for x, bw, key in ((w - 416, 100, "btn_summary"),
                           (w - 308, 108, "btn_intent"),
                           (w - 192, 118, "btn_reply"),
                           (w - 60, 40, "btn_refresh")):
            getattr(self, key).setFrame_(NSMakeRect(x, h - 48, bw, 28))
        self.left_scroll.setFrame_(NSMakeRect(20, 44, 300, h - 104))
        self.right_scroll.setFrame_(NSMakeRect(332, 44, w - 352, h - 104))
        self.status.setFrame_(NSMakeRect(22, 14, w - 44, 20))

    def windowDidResize_(self, note):
        self._layout()

    # ---------- ObjC: 分类切换 ----------

    def onSegment_(self, sender):
        self.category = CATEGORIES[sender.selectedSegment()]
        self._apply_filter()

    def _apply_filter(self):
        if self.category == "全部":
            self.visible = list(self.convs)
        else:
            self.visible = [c for c in self.convs if c["category"] == self.category]
        if self.category == "公众号":
            self.visible.sort(key=lambda c: -(c["msg_count"] or 0))   # 聚合占位行（0条）自然沉底
        self.table.reloadData()
        first = next((i for i, c in enumerate(self.visible)
                      if (c["msg_count"] or 0) > 0),
                     0 if self.visible else -1)
        if 0 <= first < len(self.visible):
            self.table.selectRowIndexes_byExtendingSelection_(
                NSIndexSet.indexSetWithIndex_(first), False)
            self.selected_username = self.visible[first]["username"]
        else:
            self.selected_username = None
        self._on_selection_changed()

    # ---------- ObjC: 表格数据源 / 代理 ----------

    def numberOfRowsInTableView_(self, tv):
        return len(self.visible)

    def tableView_objectValueForTableColumn_row_(self, tv, col, row):
        if row < 0 or row >= len(self.visible):
            return ""
        c = self.visible[row]
        ts = datetime.fromtimestamp(c["sort_ts"]).strftime("%m-%d") if c["sort_ts"] else "?"
        n = c["msg_count"]
        n_s = f"{n}条 · " if n is not None else ""
        return f"{c['display']}  ({n_s}{ts})"

    def tableViewSelectionDidChange_(self, note):
        row = self.table.selectedRow()
        if 0 <= row < len(self.visible):
            self.selected_username = self.visible[row]["username"]
        self._on_selection_changed()
        self._show_transcript()

    def _show_transcript(self):
        """选中会话 → 右侧立即显示聊天内容（含非文本占位，后台线程读取）。

        纪元号守卫：连续快速点击/切换分类时，旧线程的结果直接丢弃，
        避免慢线程覆盖新选中的会话内容（错位问题的另一根因）。
        """
        username = self.selected_username
        if not username:
            return
        conv = next((c for c in self.visible if c["username"] == username), None)
        display = conv["display"] if conv else username
        self._transcript_epoch = getattr(self, "_transcript_epoch", 0) + 1
        epoch = self._transcript_epoch
        threading.Thread(target=self._load_transcript,
                         args=(username, display, epoch), daemon=True).start()

    def _load_transcript(self, username: str, display: str, epoch: int):
        try:
            msgs = self.provider.messages(username, limit=200, include_non_text=True)
        except Exception as e:
            if epoch == getattr(self, "_transcript_epoch", epoch):
                AppHelper.callAfter(lambda: _status(self, f"读取会话失败: {e}"))
            return
        head = f"# {display} — 最近 {len(msgs)} 条消息（图片/语音以占位符显示）"
        if not msgs:
            body = "（该会话没有可展示的消息）"
        else:
            body = "\n".join(
                f"[{datetime.fromtimestamp(m['ts']).strftime('%m-%d %H:%M')}] "
                f"{m['who']}({m['name']}): {m['text']}" for m in msgs)

        def apply():
            if epoch != getattr(self, "_transcript_epoch", epoch):
                return   # 已有更新的选择，丢弃本次结果
            _result(self, f"{head}\n\n{body}", to_end=True)
            _status(self, f"已加载「{display}」的会话内容 — 点上方按钮可做分析")
        AppHelper.callAfter(apply)

    def _on_selection_changed(self):
        if not self.selected_username:
            _status(self, f"{self.category}：0 个会话")
            return
        c = next((c for c in self.visible
                  if c["username"] == self.selected_username), None)
        if c:
            # live 模式不算条数（逐会话 sqlcipher 太慢），msg_count 为 None 时不显示段
            n = f"{c['msg_count']} 条消息 · " if c["msg_count"] is not None else ""
            _status(self, f"已选「{c['display']}」— {n}"
                          f"{c['category']}，点上方按钮开始分析")

    # ---------- ObjC: 刷新 / 动作 ----------

    def refresh_(self, sender):
        try:
            self.convs = self.provider.conversations(
                250, with_counts=not getattr(self, "_live", False))
        except Exception as e:
            _status(self, f"刷新失败：{type(e).__name__}: {str(e)[:80]}")
            return
        self._apply_filter()
        # 分类按钮带上各自数量，个人/群聊不再被公众号淹没
        for i, label in enumerate(CATEGORIES):
            n = (len(self.convs) if label == "全部"
                 else sum(1 for c in self.convs if c["category"] == label))
            self.seg.setLabel_forSegment_(f"{label} {n}", i)

    def onAction_(self, sender):
        tag = sender.tag()
        if tag == TAG_REFRESH:
            self.refresh_(sender)
            return
        if self.busy:
            return
        username = self.selected_username
        if not username:
            _status(self, "先在左侧选一个会话")
            return
        conv = next(c for c in self.convs if c["username"] == username)
        _busy(self, True)
        _result(self, f"分析「{conv['display']}」中…")
        threading.Thread(target=RUNNERS[tag],
                         args=(self, conv["username"], conv["display"]),
                         daemon=True).start()


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    ctl = Controller.alloc().initWithUI_(os.environ.get("JEV_DB_DIR"))
    ctl.win.setFrameAutosaveName_("HistoryAnalyzeWindow")
    ctl.win.center()
    ctl.win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
