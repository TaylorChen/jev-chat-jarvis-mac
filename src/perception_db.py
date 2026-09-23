#!/usr/bin/env python3
"""perception_db.py — 数据库直读感知层（替代 OCR 的实验性模式）

与 perception.read_conversation 返回同构的结果，但消息来自微信 live 加密库
（live_db.LiveProvider），不再是屏幕 OCR：

  * 轮询 SessionTable 发现新活跃会话（一次查询，开销 ~50ms）
  * 只处理新到达的「对方」文本消息（触发消息），并把该会话最近
    context_limit 条真实历史一并作为 msgs 交给下游 —— 意图判断与候选
    回复天然带完整上下文
  * 图片/语音等非文本以「[图片]」占位符出现在上下文里；纯占位消息
    不作为触发
  * 会话切换走 hud 既有的 reply_key 机制，无需额外状态

环境变量：
  JEV_SOURCE=db       hud 启用本读取器（默认 ocr 维持 OCR；旧开关 JEV_DB_MODE=1 仍认）
  JEV_LIVE_DIR        live db_storage 路径（默认自动定位）
  JEV_KEYS_FILE       密钥文件路径（默认见 wechat_keys.keys_file()）
  JEV_DB_WATCH        follow（默认，你最近打开过的会话）/ all（所有会话）
"""
from __future__ import annotations

import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

sys_path = str(Path(__file__).parent)
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

DEFAULT_CATEGORIES = ("个人", "群聊", "公众号")


@dataclass
class DBReadResult:
    ok: bool
    unchanged: bool
    messages: list
    chat_title: str | None
    window: object
    fingerprint: bytes
    timing_ms: dict = field(default_factory=dict)
    error: str | None = None


class DBReader:
    def __init__(self, provider=None, live_dir=None, keys_file=None,
                 self_wxid: str | None = None, context_limit: int = 100,
                 poll_top: int = 50,
                 categories: tuple = DEFAULT_CATEGORIES,
                 window_fn=None,
                 watch: str = "follow",
                 pin: str | None = None):
        if provider is not None:
            self.provider = provider           # 测试注入用
        else:
            import os as _os
            from live_db import LiveProvider
            self.provider = LiveProvider(
                live_dir=live_dir or _os.environ.get("JEV_LIVE_DIR") or None,
                keys_file=keys_file or _os.environ.get("JEV_KEYS_FILE") or None,
                self_wxid=self_wxid)
        self.watch = watch if watch in ("follow", "all") else "follow"
        # follow: 只分析你最近打开过的会话（last_clear_unread 最新者）
        # all:    所有会话的新消息都触发（旧行为，可能很吵）
        # pin:    手动钉住一个 username（微信不为「点开一个没有未读的会话」写任何痕迹，
        #         见 set_pin 的注释），设了就只读它
        self.pin = pin or None
        self.context_limit = context_limit
        self.poll_top = poll_top
        self.categories = set(categories)
        self.last_max_ts = 0
        self.session_last: dict[str, int] = {}   # username -> 已处理的最新消息 ts
        self.pending: deque = deque()            # 发现了新活动的会话（FIFO）
        self.pending_seen: set = set()
        self.window_fn = window_fn

    def set_pin(self, username: str | None) -> None:
        """手动钉住要读的会话；传 None 恢复自动跟随。

        为什么需要它：微信只在**清未读**时写 `last_clear_unread_timestamp`。点开一个
        「没有未读」的会话（最常见的复习场景）时数据库里没有任何痕迹——实测点开
        「阿虎-安增辉」（未读=0）后该时间戳仍停在几十分钟前，于是自动跟随永远选中那个
        刚清过未读的群。AX 也拿不到会话列表（微信只暴露窗口按钮，AXEnhancedUserInterface
        不支持），所以这一步只能交给用户显式指定。
        """
        self.pin = username or None
        self.pending.clear()
        self.pending_seen.clear()
        self.last_target = None      # 让下一跳按「切换会话」处理，面板立刻跟过去

    # ---------- 轮询：发现新活跃会话 ----------

    def _discover(self) -> None:
        if self.pin:
            if self.pin not in self.pending_seen:
                self.pending.append(self.pin)
                self.pending_seen.add(self.pin)
            return
        if self.watch == "follow":
            # 目标 = 你最近打开过的会话。**不能只在「最近活跃的前 N 个」里挑**：点开一个
            # 许久没消息的老会话时（它的 sort_timestamp 很旧、根本不在窗口里），面板会一直
            # 读上一个会话，用户以为「点了没反应」。直接问全表谁的 clear 最新就够了——
            # SessionTable 是千行级，一次查询的代价与原来相同。
            rows = self.provider._query(
                "session/session.db",
                "SELECT username FROM SessionTable "
                "WHERE last_clear_unread_timestamp > 0 "
                "ORDER BY last_clear_unread_timestamp DESC LIMIT 1")
            if rows:
                u = rows[0]["username"]
                if u not in self.pending_seen:
                    self.pending.append(u)
                    self.pending_seen.add(u)
            return
        rows = self.provider._query(
            "session/session.db",
            "SELECT username, sort_timestamp, last_clear_unread_timestamp "
            f"FROM SessionTable ORDER BY sort_timestamp DESC LIMIT {int(self.poll_top)}")
        newest = 0
        for r in rows:
            username = r["username"]
            ts = int(r["sort_timestamp"] or 0)
            newest = max(newest, ts)
            if ts > self.last_max_ts and self.provider.category(username) in self.categories:
                if username not in self.pending_seen:
                    self.pending.append(username)
                    self.pending_seen.add(username)
        self.last_max_ts = max(self.last_max_ts, newest)

    # ---------- 主入口（与 perception.read_conversation 同构） ----------

    def read_conversation(self, max_messages: int = 12, previous_wid=None,
                          prev_fingerprint=None, window=None) -> dict:
        t0 = time.perf_counter()
        if window is None:
            try:
                from perception import find_wechat_window
                w = find_wechat_window(previous_wid)
                window = {"wid": w.wid, "pid": w.pid, "title": w.title,
                          "x": w.x, "y": w.y, "w": w.w, "h": w.h} if w else None
            except Exception:
                window = None
        if window is None:
            # 微信窗口不在屏幕上：合成占位几何 dict（面板固定；填入不可用），
            # 消息仍然来自数据库，不中断分析。synthetic 让下游知道这不是真窗口——
            # 别拿它去做 AX 定位或截屏兜底（既无意义，又会白白触发截屏权限提示）。
            window = {"wid": previous_wid or 0, "pid": 0, "title": "",
                      "x": 200.0, "y": 200.0, "w": 800.0, "h": 600.0,
                      "synthetic": True}
        self._discover()

        if not self.pending:
            # 无任何会话活动：恒 unchanged，让下游走等待短路
            return DBReadResult(ok=True, unchanged=True, messages=[],
                                chat_title=None, window=window,
                                fingerprint=f"db:idle:{int(self.last_max_ts)}".encode(),
                                timing_ms={"db": 0}).__dict__

        username = self.pending.popleft()
        self.pending_seen.discard(username)
        target_changed = (username != getattr(self, "last_target", username))
        msgs_all = self.provider.messages(username, limit=self.context_limit,
                                          include_non_text=True)
        last_seen = self.session_last.get(username, 0)
        their_all = [m for m in msgs_all if m["who"] == "对方"
                     and not m["text"].startswith("[")]
        new_incoming = [m for m in their_all if m["ts"] > last_seen]

        display = self.provider.display_name(username)
        follow_switch = (self.watch == "follow" and target_changed)
        if new_incoming:
            trigger = new_incoming[-1]              # 有新对方文本：正常触发
        elif their_all and follow_switch:
            # 你切换了会话但没有更新的对方消息：展示该会话最新的对方消息分析
            trigger = their_all[-1]
        else:
            trigger = None
        self.last_target = username
        if trigger is None:
            newest_ts = max((int(m["ts"]) for m in msgs_all), default=0)
            fp = f"db:noop:{username}:{newest_ts}".encode()
            # 切到一个「没有可回复的对方消息」的会话（比如最后一句是我说的，或最近 100 条
            # 里没有对方文本）时，也要让面板跟着走：报 unchanged 的话 hud 会复用上一会话的
            # 内容，用户点了半天以为没反应。同一会话的重复空转仍报 unchanged，免得每跳都
            # 把面板清一次。all 模式不按「切换」语义，保持原样。
            switched = (self.watch == "follow" and target_changed)
            return DBReadResult(ok=True, unchanged=not switched, messages=[],
                                chat_title=display, window=window,
                                fingerprint=fp, timing_ms={"db": 0}).__dict__
        self.session_last[username] = trigger["ts"]   # 记录已处理的触发消息时间
        idx = next(i for i, m in enumerate(msgs_all)
                   if m["ts"] == trigger["ts"] and m["text"] == trigger["text"])
        sliced = msgs_all[:idx + 1][-self.context_limit:]

        fp = f"db:{username}:{trigger['ts']}:{trigger['text'][:32]}".encode()
        unchanged = (fp == prev_fingerprint)

        return DBReadResult(
            ok=True,
            unchanged=unchanged,
            messages=[_to_hud_message(m) for m in sliced],
            chat_title=display,
            window=window,
            fingerprint=fp,
            timing_ms={"db": round((time.perf_counter() - t0) * 1000)},
        ).__dict__


def _to_hud_message(m: dict):
    """DB 消息 → perception.Message 形状（side/sender/text/ts；几何字段置零）。"""
    from perception import Message
    return Message(
        text=m["text"],
        side="me" if m["who"] == "我" else "them",
        y=0.0,
        conf=1.0,
        sender=None if m["who"] == "我" else (m["name"] or None),
        lines=[m["text"]],
        ts=int(m.get("ts") or 0),
        img_path=m.get("img_path"),
    )
