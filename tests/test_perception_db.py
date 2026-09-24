import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from perception_db import DBReader  # noqa: E402


class FakeProvider:
    """与 LiveProvider 鸭子兼容的假供给者（覆盖 DBReader 用到的面）。"""

    def __init__(self, sessions, msgs):
        # sessions: [(username, sort_timestamp, last_clear_unread_timestamp)]
        self._sessions = sessions
        self._msgs = msgs
        self.names = {"boss_a": "张三", "dev_group": "开发者群", "quiet": "静静",
                      "noisy_group": "刷屏群", "quiet_history": "历史联系人"}

    def _query(self, rel, sql):
        assert rel == "session/session.db"
        rows = [{"username": u, "sort_timestamp": ts,
                 "last_clear_unread_timestamp": clear}
                for u, ts, clear in self._sessions]
        if "last_clear_unread_timestamp > 0" in sql:        # follow：全表按「清未读」取最新
            rows = [r for r in rows if r["last_clear_unread_timestamp"]]
            rows.sort(key=lambda r: -int(r["last_clear_unread_timestamp"]))
        else:                                              # all：按最近活跃取窗口
            rows.sort(key=lambda r: -int(r["sort_timestamp"]))
            limit = re.search(r"LIMIT (\d+)", sql)
            if limit:
                rows = rows[:int(limit.group(1))]
        return rows

    def category(self, u):
        if "@chatroom" in u:
            return "群聊"
        if u.startswith("gh_"):
            return "公众号"
        return "个人"

    def display_name(self, u):
        return self.names.get(u, u)

    def messages(self, u, days=None, limit=500, include_non_text=True):
        return list(self._msgs.get(u, []))


def msg(ts, who, text, name=""):
    return {"ts": ts, "who": who, "name": name, "text": text}


class DBReaderTests(unittest.TestCase):
    def _reader(self, sessions=None, msgs=None, watch="follow"):
        return DBReader(provider=FakeProvider(sessions or [], msgs or {}),
                        context_limit=100, watch=watch)

    # ---------- follow 模式（默认） ----------

    def test_follow_targets_recently_opened(self):
        """follow：目标 = 你最近打开过的会话（清未读时间最新）。"""
        rd = self._reader(
            [("boss_a", 200, 150), ("dev_group", 150, 100)],
            {"boss_a": [msg(200, "对方", "老板的消息", "张三")],
             "dev_group": [msg(190, "对方", "群里有人说话", "张三")]})
        r = rd.read_conversation()
        self.assertEqual(r["chat_title"], "张三")
        self.assertFalse(r["unchanged"])
        self.assertEqual(r["messages"][-1].text, "老板的消息")
        self.assertEqual(r["messages"][-1].side, "them")

    def test_follow_switches_when_you_open_another(self):
        """follow：切换打开的会话 → 面板跟随到新会话。"""
        rd = self._reader(
            [("boss_a", 200, 150), ("dev_group", 150, 100)],
            {"boss_a": [msg(200, "对方", "老板的消息", "张三")],
             "dev_group": [msg(190, "对方", "群里有人说话", "张三")]})
        r1 = rd.read_conversation()
        self.assertEqual(r1["chat_title"], "张三")
        # 你切去 dev_group（它的清未读时间变成最新，且来了新消息）
        rd.provider._sessions = [("dev_group", 300, 200), ("boss_a", 200, 100)]
        rd.provider._msgs["dev_group"].append(msg(300, "对方", "群里又有人说话", "李四"))
        r2 = rd.read_conversation()
        self.assertEqual(r2["chat_title"], "开发者群")

    def test_own_messages_never_trigger(self):
        """只有自己发言（无新对方消息）→ 不触发。"""
        r = self._reader(
            [("boss_a", 100, 90)],
            {"boss_a": [msg(100, "我", "我自己发的消息")]}).read_conversation()
        self.assertTrue(r["unchanged"])
        self.assertEqual(r["messages"], [])

    def test_non_text_placeholder_never_triggers(self):
        """纯 [图片] 占位的新消息 → 不触发（无法判断意图）。"""
        r = self._reader(
            [("boss_a", 100, 90)],
            {"boss_a": [msg(100, "对方", "[图片]", "张三")]}).read_conversation()
        self.assertTrue(r["unchanged"])

    def test_follow_finds_a_chat_outside_the_recent_window(self):
        """点开一个「很久没消息」的老会话：它不在按最近活跃的窗口里，也必须被选中。

        用户问题原话：会话没有新消息时点它，怎么匹配？——微信点开就会写 clear 时间戳，
        所以只要按全表 clear 排序就能跟上；按 sort 取前 N 个的老写法会一直读上一个会话。
        """
        sessions = [("hot_group", 9000, 10)] + [(f"old_{i}", 1000 - i, 100 - i) for i in range(60)]
        sessions.append(("老会话", 500, 8000))          # 最后消息很旧，但你刚点开
        rd = self._reader(sessions, {"老会话": [msg(500, "对方", "翻回来看这条", "老张")]})
        r = rd.read_conversation()
        self.assertEqual(r["chat_title"], "老会话")        # display_name(username) 的回退
        self.assertEqual(r["messages"][-1].sender, "老张")
        self.assertFalse(r["unchanged"])
        self.assertEqual(r["messages"][-1].text, "翻回来看这条")

    def test_switching_to_a_chat_with_nothing_to_answer_still_moves_the_panel(self):
        """点到「最后一句是我说的」会话：没有可回复的对方消息，但面板必须跟着切过去。"""
        rd = self._reader(
            [("boss_a", 200, 950), ("quiet", 100, 900)],
            {"boss_a": [msg(200, "对方", "老板的消息", "张三")],
             "quiet": [msg(100, "我", "我说了最后一句")]})
        r1 = rd.read_conversation()
        self.assertEqual(r1["chat_title"], "张三")
        rd.provider._sessions = [("boss_a", 200, 100), ("quiet", 100, 990)]
        r2 = rd.read_conversation()                     # clear 最新变成了 quiet
        self.assertEqual(r2["chat_title"], "静静")
        self.assertFalse(r2["unchanged"], "切换会话必须报「变了」，否则面板还停在上一会话")
        self.assertEqual(r2["messages"], [])
        r3 = rd.read_conversation()                     # 同一会话再读：不重复刷面板
        self.assertTrue(r3["unchanged"])

    def test_pin_overrides_the_clear_time_heuristic(self):
        """点开一个「没有未读」的会话时微信不写任何痕迹，只能手动钉住它。"""
        rd = self._reader(
            [("noisy_group", 9000, 9999), ("quiet_history", 500, 100)],
            {"noisy_group": [msg(9000, "对方", "群里在刷屏", "群友")],
             "quiet_history": [msg(500, "对方", "好的", "历史联系人")]})
        self.assertEqual(rd.read_conversation()["chat_title"], "刷屏群")   # 自动：clear 最大者
        rd.set_pin("quiet_history")
        r = rd.read_conversation()
        self.assertEqual(r["chat_title"], "历史联系人")
        self.assertEqual(r["messages"][-1].text, "好的")
        self.assertFalse(r["unchanged"], "钉住后第一次读必须按「切换」处理，面板才会跟过去")

    def test_unpin_returns_to_auto_follow(self):
        rd = self._reader(
            [("noisy_group", 9000, 9999), ("quiet", 500, 100)],
            {"noisy_group": [msg(9000, "对方", "群里在刷屏", "群友")],
             "quiet": [msg(500, "对方", "好的", "静静")]})
        rd.set_pin("quiet")
        self.assertEqual(rd.read_conversation()["chat_title"], "静静")
        rd.set_pin(None)
        self.assertEqual(rd.read_conversation()["chat_title"], "刷屏群")

    def test_pinned_session_with_nothing_to_answer_still_reports_the_switch(self):
        rd = self._reader(
            [("noisy_group", 9000, 9999), ("quiet", 500, 100)],
            {"noisy_group": [msg(9000, "对方", "群里在刷屏", "群友")],
             "quiet": [msg(500, "我", "最后一句是我说的")]})
        rd.set_pin("quiet")
        r = rd.read_conversation()
        self.assertEqual(r["chat_title"], "静静")
        self.assertFalse(r["unchanged"])
        self.assertEqual(r["messages"], [])

    def test_trigger_slices_context_at_trigger(self):
        """触发消息之后自己又发了新消息 → 上下文截到触发消息为止。"""
        r = self._reader(
            [("boss_a", 120, 90)],
            {"boss_a": [
                msg(100, "对方", "先看方案", "张三"),
                msg(110, "对方", "方案在这", "张三"),
                msg(120, "我", "收到，我看下"),
            ]}).read_conversation()
        # 110 的「方案在这」比 last_seen(0) 新 → 触发消息 = 110 那条
        self.assertEqual(r["messages"][-1].text, "方案在这")
        self.assertNotIn("收到，我看下", [m.text for m in r["messages"]])

    def test_same_message_not_retriggered(self):
        """同一条消息不重复触发（第二次读取 unchanged）。"""
        rd = self._reader(
            [("boss_a", 100, 90)],
            {"boss_a": [msg(100, "对方", "帮我看看这个方案", "张三")]})
        r1 = rd.read_conversation()
        self.assertFalse(r1["unchanged"])
        r2 = rd.read_conversation()
        self.assertTrue(r2["unchanged"])
        self.assertEqual(r2["messages"], [])

    def test_idle_reads_stay_unchanged(self):
        """无任何会话有消息 → 每次读取都是 unchanged（下游短路）。"""
        rd = self._reader([("boss_a", 100, 90)], {})
        r1 = rd.read_conversation()
        r2 = rd.read_conversation()
        self.assertTrue(r1["unchanged"])
        self.assertTrue(r2["unchanged"])
        self.assertEqual(r1["messages"], [])
        self.assertEqual(r2["messages"], [])

    # ---------- all 模式（旧行为） ----------

    def test_all_mode_fifo(self):
        """watch=all：两个会话同时有新消息 → FIFO 先后处理。"""
        rd = self._reader(
            [("boss_a", 200, 90), ("dev_group", 190, 90)],
            {"boss_a": [msg(200, "对方", "老板的消息", "张三")],
             "dev_group": [msg(190, "对方", "群里有人说话", "张三")]},
            watch="all")
        r1 = rd.read_conversation()
        self.assertEqual(r1["chat_title"], "张三")
        r2 = rd.read_conversation()
        self.assertEqual(r2["chat_title"], "开发者群")


if __name__ == "__main__":
    unittest.main()
