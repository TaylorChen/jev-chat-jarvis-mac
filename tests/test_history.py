import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from history import HistoryProvider  # noqa: E402

try:
    import zstandard as zstd
    HAVE_ZSTD = True
except ImportError:
    HAVE_ZSTD = False

SELF = "wxid_selftest"
TEXT = 1


def _msg_table(db, username, rows):
    """rows: [(real_sender_id, sort_seq, content, ct)]，时间正序写入。"""
    table = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
    db.execute(
        f"CREATE TABLE {table}(local_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        f"server_id INTEGER, local_type INTEGER, sort_seq INTEGER, "
        f"real_sender_id INTEGER, create_time INTEGER, status INTEGER, "
        f"upload_status INTEGER, download_status INTEGER, server_seq INTEGER, "
        f"origin_source INTEGER, source TEXT, message_content TEXT, "
        f"compress_content TEXT, packed_info_data BLOB, "
        f"WCDB_CT_message_content INTEGER DEFAULT NULL, WCDB_CT_source INTEGER DEFAULT NULL)")
    for sender, seq, content, ct in rows:
        db.execute(
            f"INSERT INTO {table}(local_type, sort_seq, real_sender_id, "
            f"message_content, WCDB_CT_message_content) VALUES (?,?,?,?,?)",
            (TEXT, seq, sender, content, ct))


class HistoryProviderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jev-hist-"))
        (self.dir / "contact").mkdir()
        (self.dir / "session").mkdir()
        (self.dir / "message").mkdir()

        con = sqlite3.connect(self.dir / "contact/contact.db")
        con.execute("CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, "
                    "alias TEXT, remark TEXT, nick_name TEXT, verify_flag INTEGER)")
        con.executemany("INSERT INTO contact(username, remark, nick_name, verify_flag) VALUES (?,?,?,?)", [
            ("boss_a", "张三", "磊哥", 0),
            ("boss_b", "张三", "老王", 0),
            ("quiet_king", "张三", "张三", 0),
            ("987654@chatroom", "", "开发者群", 0),
            ("wxid_media01", "", "央视新闻测试", 1048),
        ])
        con.commit()
        con.close()

        ses = sqlite3.connect(self.dir / "session/session.db")
        ses.execute("CREATE TABLE SessionTable(username TEXT PRIMARY KEY, type INTEGER, "
                    "unread_count INTEGER, is_hidden INTEGER, summary TEXT, draft TEXT, "
                    "status INTEGER, last_timestamp INTEGER, sort_timestamp INTEGER, "
                    "last_clear_unread_timestamp INTEGER, last_msg_locald_id INTEGER, "
                    "last_msg_type INTEGER, last_msg_sub_type INTEGER, last_msg_sender TEXT, "
                    "last_sender_display_name TEXT, last_msg_ext_type INTEGER, "
                    "unread_first_msg_srv_id INTEGER, unread_first_pat_msg_local_id INTEGER, "
                    "unread_first_pat_msg_sort_seq INTEGER)")
        ses.execute("INSERT INTO SessionTable(username, sort_timestamp) VALUES (?,?)",
                    ("boss_a", 2000))
        ses.execute("INSERT INTO SessionTable(username, sort_timestamp) VALUES (?,?)",
                    ("boss_b", 1000))
        ses.execute("INSERT INTO SessionTable(username, sort_timestamp) VALUES (?,?)",
                    ("987654@chatroom", 3000))
        ses.execute("INSERT INTO SessionTable(username, sort_timestamp) VALUES (?,?)",
                    ("gh_official", 4000))
        ses.commit()
        ses.close()

        msg = sqlite3.connect(self.dir / "message/message_0.db")
        msg.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
        for i, u in enumerate((SELF, "boss_a", "boss_b", "quiet_king", "987654@chatroom",
                               "gh_official", "wxid_media01"), 1):
            msg.execute("INSERT INTO Name2Id(user_name, is_session) VALUES (?,0)", (u,))
        # 两个「张三」各自的消息；boss_b 里有一条唯一文本（模拟屏幕上的触发消息）
        _msg_table(msg, "boss_a", [
            (2, 1, "王-小助手:\n周末团建去爬山吗", 0),
            (1, 2, "王-小助手:\n记得带外套", 0),
        ])
        rows_b = [
            (3, 1, "boss_b:\n这个需求今天能给排期吗", 0),
            (1, 2, "boss_b:\n客户端那边还等着联调", 0),
        ]
        if HAVE_ZSTD:
            comp = zstd.ZstdCompressor().compress("boss_b:\n压缩过的这句话也能搜到".encode())
            rows_b.append((3, 3, comp, 4))
        _msg_table(msg, "boss_b", rows_b)
        _msg_table(msg, "987654@chatroom", [
            (5, 1, "张三:\n群里好", 0),
            (6, 2, "李四:\n晚上开会", 0),
        ])
        # 公众号消息表在独立的 biz_message 库（同名 md5 命名，会话只属一个库）
        (self.dir / "message/biz_message_0.db").unlink(missing_ok=True)
        biz = sqlite3.connect(self.dir / "message/biz_message_0.db")
        biz.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
        biz.execute("INSERT INTO Name2Id(user_name) VALUES ('gh_official')")
        _msg_table(biz, "gh_official", [
            (7, 1, "公众号推文卡片内容摘要", 0),
            (8, 2, "另一篇推文", 0),
        ])
        # 夹具补时间戳（真实库 create_time 恒有值）：sort_seq 越大越新
        for t in ("boss_a", "boss_b", "987654@chatroom"):
            table = f"Msg_{hashlib.md5(t.encode()).hexdigest()}"
            msg.execute(f"UPDATE {table} SET create_time = 1700000000 + sort_seq * 3600")
        gh_table = f"Msg_{hashlib.md5('gh_official'.encode()).hexdigest()}"
        biz.execute(f"UPDATE {gh_table} SET create_time = 1700000000 + sort_seq * 3600")
        msg.commit()
        msg.close()
        biz.commit()
        biz.close()

        self.p = HistoryProvider(self.dir, self.self_wxid())

    def self_wxid(self):
        return SELF

    def tearDown(self):
        self.p.close()

    def test_same_title_returns_all_candidates(self):
        """同名「张三」三个候选都要返回（撞名是常态，不能静默取第一个）。"""
        cands = self.p._candidates_by_title("张三")
        self.assertEqual(set(cands), {"boss_a", "boss_b", "quiet_king"})

    def test_l1_text_disambiguates_same_title(self):
        """同名 + OCR 触发文本 → 唯一命中真正在屏的那个会话。"""
        got = self.p.resolve("张三", "这个需求今天能给排期吗")
        self.assertEqual(got, "boss_b")
        got = self.p.resolve("张三", "周末团建去爬山吗")
        self.assertEqual(got, "boss_a")

    def test_l1_sees_through_zstd_rows(self):
        """WCDB_CT=4 的 zstd 行也要参与内容反查。"""
        if not HAVE_ZSTD:
            self.skipTest("zstandard 未安装")
        got = self.p.resolve("张三", "压缩过的这句话也能搜到")
        self.assertEqual(got, "boss_b")

    def test_l3_ambiguous_without_text_returns_none(self):
        """同名、两候选活跃度接近（<24h）且无文本 → 宁可放弃不硬猜。"""
        self.assertIsNone(self.p.resolve("张三", ""))

    def test_l2_recency_picks_when_clearly_fresher(self):
        """无文本但第一候选活跃度领先 >24h → 采信最近的。"""
        ses = sqlite3.connect(self.dir / "session/session.db")
        ses.execute("UPDATE SessionTable SET sort_timestamp=1000 WHERE username='boss_b'")
        ses.execute("UPDATE SessionTable SET sort_timestamp=200000 WHERE username='boss_a'")
        ses.commit()
        ses.close()
        self.p._session = sqlite3.connect(f"file:{self.dir}/session/session.db?mode=ro", uri=True)
        self.assertEqual(self.p.resolve("张三", ""), "boss_a")

    def test_context_roles_and_sender_prefix_stripped(self):
        ctx = self.p.context_for("boss_b", n=10)
        self.assertIsNotNone(ctx)
        roles = [l.split(":", 1)[0] for l in ctx.splitlines() if ":" in l]
        self.assertIn("我", roles)
        self.assertTrue(any(r.startswith("对方") for r in roles))
        # 群昵称前缀（boss_b:）不能残留在正文里
        self.assertNotIn("boss_b:", ctx)

    def test_resolve_short_text_does_not_guess(self):
        """太短的触发文本（<4字）不可靠 → 不做内容反查，直接降级。"""
        self.assertIsNone(self.p._filter_by_text(["boss_a", "boss_b"], "好的"))

    def test_messages_roles_order_and_days_filter(self):
        msgs = self.p.messages("boss_b")
        self.assertEqual([m["who"] for m in msgs], ["对方", "我", "对方"])
        self.assertEqual([m["name"] for m in msgs], ["boss_b", "我", "boss_b"])
        self.assertTrue(all(m["text"] and ":\n" not in m["text"] for m in msgs))
        future_only = self.p.messages("boss_b", days=10 ** 6)
        self.assertEqual(len(future_only), 3)
        none_yet = self.p.messages("boss_b", days=-1)   # 截断时间在未来 → 全部排除
        self.assertEqual(none_yet, [])

    def test_conversations_lists_display_name_and_count(self):
        convs = {c["username"]: c for c in self.p.conversations(limit=10)}
        self.assertIn("boss_a", convs)
        self.assertEqual(convs["boss_a"]["display"], "张三")
        self.assertEqual(convs["boss_a"]["msg_count"], 2)
        self.assertGreaterEqual(convs["boss_a"]["sort_ts"], convs["boss_b"]["sort_ts"])

    def test_category_and_biz_db_scan(self):
        """分类正确；公众号表在 biz_message 库里也能被找到并计数。"""
        self.assertEqual(self.p.category("boss_a"), "个人")
        self.assertEqual(self.p.category("123@chatroom"), "群聊")
        self.assertEqual(self.p.category("gh_official"), "公众号")
        self.assertEqual(self.p.category("brandsessionholder"), "公众号")
        # wxid 格式的认证官方号（如央视新闻）也要识别为公众号
        self.assertEqual(self.p.category("wxid_media01"), "公众号")
        convs = {c["username"]: c for c in self.p.conversations(limit=10)}
        self.assertEqual(convs["987654@chatroom"]["category"], "群聊")
        # gh_official 的表在 biz 库：也能定位，且两个库中的行都计入
        self.assertEqual(convs["gh_official"]["msg_count"], 2)
        texts = [m["text"] for m in self.p.messages("gh_official")]
        self.assertIn("公众号推文卡片内容摘要", texts)
        self.assertIn("另一篇推文", texts)

    def test_display_name_prefers_remark(self):
        self.assertEqual(self.p.display_name("boss_a"), "张三")
        self.assertEqual(self.p.display_name("nobody"), "nobody")


if __name__ == "__main__":
    unittest.main()


class ResultScrollTests(unittest.TestCase):
    """会话内容滚到最新一条；分析结果仍从顶部读（总结第一行就是标题）。"""

    def _ctl(self):
        ctl = SimpleNamespace(text=Mock())
        ctl.text.textStorage.return_value.length.return_value = 123
        return ctl

    def test_transcript_scrolls_to_the_newest_line(self):
        import history_ui
        ctl = self._ctl()
        history_ui._result(ctl, "会话内容", to_end=True)
        ctl.text.scrollToEndOfDocument_.assert_called_once()
        ctl.text.setSelectedRange_.assert_called_once()

    def test_analysis_result_stays_at_the_top(self):
        import history_ui
        ctl = self._ctl()
        history_ui._result(ctl, "## 议题概览")
        ctl.text.scrollToEndOfDocument_.assert_not_called()
        ctl.text.scrollRangeToVisible_.assert_called_once()


if __name__ == '__main__':
    unittest.main()
