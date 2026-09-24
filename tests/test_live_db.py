"""live_db 的离线回归：不碰微信库、不启动 sqlcipher。

覆盖两类曾经真实踩过的坑：
  * sqlcipher 的 PRAGMA key 引号必须精确，错位会伪装成「密钥不对」；
  * 消息库 Name2Id 的列名是 user_name（contact.db 才是 username），
    写错不会报错，只会让每条消息都被判成对方。

真库的实时性/结构由 probe/live_db_smoke.py 冒烟（需要微信在跑）。
Run: python -B -m unittest discover -s tests
"""
import hashlib
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import live_db  # noqa: E402
from history import HistoryProvider  # noqa: E402
from live_db import (CIPHER_PRAGMAS, LiveProvider, _looks_like_raw_id,  # noqa: E402
                     LiveQueryError, _resolve_sqlcipher, _row_content,
                     _strip_sender_prefix, key_script)

try:
    import zstandard
    HAVE_ZSTD = True
except ImportError:
    HAVE_ZSTD = False


class KeyScriptTests(unittest.TestCase):
    """PRAGMA key 的引号是这里唯一的坑，钉死它。"""

    def test_key_line_quotes_the_raw_key_exactly(self):
        first = key_script('aabbcc', 'SELECT 1').splitlines()[0]
        self.assertEqual(first, 'PRAGMA key = "x\'aabbcc\'";')

    def test_semicolon_is_outside_the_quotes(self):
        # 写成 "x'<key>';" 时 sqlcipher 把分号当进字面量：
        # near "PRAGMA": syntax error + file is not a database，看起来像密钥错
        self.assertNotIn("';\"", key_script('aabbcc', 'SELECT 1'))

    def test_pragmas_and_json_mode_are_present(self):
        script = key_script('aabbcc', 'SELECT 1')
        for line in CIPHER_PRAGMAS.strip().splitlines():
            self.assertIn(line, script)
        self.assertIn('.mode json', script)

    def test_query_is_terminated_once(self):
        self.assertTrue(key_script('k', 'SELECT 1').endswith('SELECT 1;\n'))
        self.assertTrue(key_script('k', 'SELECT 1;\n').endswith('SELECT 1;\n'))


class LiveQueryErrorTests(unittest.TestCase):
    def _provider(self, root):
        provider = LiveProvider.__new__(LiveProvider)
        provider.live_dir = Path(root)
        provider.keys = {'message/message_0.db': {'enc_key': '00'}}
        provider._db_tables = {}
        path = provider.live_dir / 'message/message_0.db'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'db')
        return provider

    def test_successful_empty_query_is_distinct_from_failure(self):
        with tempfile.TemporaryDirectory() as root:
            provider = self._provider(root)
            for stdout in ('', 'ok\n'):
                result = SimpleNamespace(returncode=0, stdout=stdout, stderr='')
                with self.subTest(stdout=stdout), \
                        patch('live_db.subprocess.run', return_value=result):
                    self.assertEqual(provider._query('message/message_0.db', 'SELECT 1'), [])

    def test_nonzero_and_malformed_results_raise_query_error(self):
        with tempfile.TemporaryDirectory() as root:
            provider = self._provider(root)
            for result in (SimpleNamespace(returncode=1, stdout='', stderr='bad key'),
                           SimpleNamespace(returncode=0, stdout='not json', stderr='')):
                with self.subTest(result=result), \
                        patch('live_db.subprocess.run', return_value=result), \
                        self.assertRaises(LiveQueryError):
                    provider._query('message/message_0.db', 'SELECT 1', attempts=1)

    def test_failed_table_scan_is_never_cached_as_an_empty_database(self):
        with tempfile.TemporaryDirectory() as root:
            provider = self._provider(root)
            provider._query = Mock(side_effect=LiveQueryError('read failed'))
            with self.assertRaises(LiveQueryError):
                provider._tables_of('message/message_0.db')
            self.assertNotIn('message/message_0.db', provider._db_tables)


class RowContentTests(unittest.TestCase):
    def test_plain_row_is_returned_as_is(self):
        self.assertEqual(_row_content(0, '你好'), '你好')
        self.assertEqual(_row_content(0, None), '')

    def test_compressed_row_without_payload_is_empty(self):
        self.assertEqual(_row_content(4, None), '')
        self.assertEqual(_row_content(4, ''), '')

    def test_broken_hex_or_frame_is_empty_not_garbage(self):
        # 解不开就跳过该行：绝不能把压缩帧当明文塞进上下文
        self.assertEqual(_row_content(4, 'zz'), '')
        self.assertEqual(_row_content(4, '00'), '')

    @unittest.skipUnless(HAVE_ZSTD, 'zstandard 未安装')
    def test_wcdb_zstd_frame_roundtrips(self):
        frame = zstandard.ZstdCompressor().compress('周末团建去爬山吗'.encode())
        self.assertEqual(_row_content(4, frame.hex()), '周末团建去爬山吗')


class SenderPrefixTests(unittest.TestCase):
    def test_group_message_splits_nickname(self):
        self.assertEqual(_strip_sender_prefix('张三:\n晚上开会'), ('张三', '晚上开会'))

    def test_plain_message_has_no_prefix(self):
        self.assertEqual(_strip_sender_prefix('晚上开会'), ('', '晚上开会'))

    def test_multiline_head_is_not_a_nickname(self):
        self.assertEqual(_strip_sender_prefix('第一行\n第二行:\n正文'), ('', '第一行\n第二行:\n正文'))

    def test_overlong_head_is_not_a_nickname(self):
        text = 'x' * 65 + ':\n正文'
        self.assertEqual(_strip_sender_prefix(text), ('', text))


def _md5_table(username: str) -> str:
    return f"Msg_{hashlib.md5(username.encode()).hexdigest()}"


class FakeLive(LiveProvider):
    """只替换 _query 的 LiveProvider：分片/缓存/自己 id 的逻辑都能离线跑。"""

    def __init__(self, live_dir, keys, tables, name2id=None):
        self.live_dir = Path(live_dir)
        self.keys = keys
        self._tables = tables            # rel -> 表名集合；不在其中 = 读失败（返回 []）
        self._name2id = name2id or {}    # rel -> {'user_name': rowid}（按列名索引）
        self.queries = []
        self._db_tables = {}
        self._self_ids = {}
        self.self_wxid = 'me_id'

    def _query(self, rel, sql):
        self.queries.append(sql)
        if 'Name2Id' in sql:
            for col, rid in self._name2id.get(rel, {}).items():
                if f'{col}=' in sql:
                    return [{'rid': rid}]
            return []
        if 'sqlite_master' in sql:
            return [{'name': n} for n in sorted(self._tables.get(rel, ()))]
        if 'COUNT(*)' in sql:
            return [{'c': self._tables.get(rel, ()) and 7}]
        return []

    def count_queries(self, needle):
        return sum(1 for q in self.queries if needle in q)


class ShardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='jev-live-')
        (Path(self.tmp) / 'message').mkdir()
        self.user = 'boss_a'
        self.table = _md5_table(self.user)
        rels = {}
        for name, tables in (('message_0.db', {self.table}),
                             ('message_1.db', {self.table, _md5_table('other')}),
                             ('media_0.db', set()),
                             ('message_fts.db', set())):
            (Path(self.tmp) / 'message' / name).write_bytes(b'')
            rels[f'message/{name}'] = {'enc_key': '00' * 16, 'tables': tables}
        self.p = FakeLive(self.tmp, {k: {'enc_key': v['enc_key']} for k, v in rels.items()},
                          {k: v['tables'] for k, v in rels.items()})

    def test_shards_are_md5_table_matches_across_dbs(self):
        self.assertEqual(self.p._shards_for(self.user),
                         [('message/message_0.db', self.table),
                          ('message/message_1.db', self.table)])

    def test_shard_scan_is_cached_after_the_first_call(self):
        self.p._shards_for(self.user)
        first = self.p.count_queries('sqlite_master')
        self.assertEqual(first, 2)          # media_0 不是消息分片，不能拿消息 key 规则扫描
        self.p._shards_for(self.user)
        self.assertEqual(self.p.count_queries('sqlite_master'), first,
                         '热路径不该再开 sqlcipher')

    def test_failed_scan_is_not_repeated_on_every_poll(self):
        # media_0.db 空集合（模拟读失败）：60s 内不再重扫，否则每跳都要几十个子进程
        self.p._shards_for(self.user)
        before = self.p.count_queries('sqlite_master')
        for _ in range(5):
            self.p._shards_for('other')
        self.assertEqual(self.p.count_queries('sqlite_master'), before)

    def test_missing_key_file_entry_is_skipped(self):
        self.p.keys.pop('message/message_1.db')
        self.assertEqual([r for r, _t in self.p._shards_for(self.user)],
                         ['message/message_0.db'])

    def test_non_message_databases_are_never_scanned_for_msg_tables(self):
        self.p._shards_for(self.user)
        self.assertEqual(self.p.count_queries('sqlite_master'), 2)


class SelfIdTests(unittest.TestCase):
    """认不出自己 = 每条消息都被当成对方，是静默错方向的故障。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='jev-live-')
        (Path(self.tmp) / 'message').mkdir()
        (Path(self.tmp) / 'message' / 'message_0.db').write_bytes(b'')
        self.rel = 'message/message_0.db'

    def _provider(self, name2id):
        return FakeLive(self.tmp, {self.rel: {'enc_key': '00'}}, {self.rel: set()},
                        {self.rel: name2id})

    def test_user_name_column_is_queried_first(self):
        p = self._provider({'user_name': 16})
        self.assertEqual(p._self_id_of_rel(self.rel), 16)
        self.assertIn('user_name=',
                      self._first_name2id_sql(p) or '', '消息库 Name2Id 的列是 user_name')

    def test_username_fallback_still_resolves(self):
        # 老库/变体列名：退一步试 username，而不是认不出来就静默全判对方
        p = self._provider({'username': 17})
        self.assertEqual(p._self_id_of_rel(self.rel), 17)

    def test_unresolvable_self_id_is_not_cached(self):
        p = self._provider({})
        self.assertIsNone(p._self_id_of_rel(self.rel))
        self.assertIsNone(p._self_id_of_rel(self.rel))
        self.assertEqual(p.count_queries('Name2Id'), 4)   # 两种列名 × 两次调用

    def test_startup_probe_ignores_media_databases_without_name2id(self):
        p = LiveProvider.__new__(LiveProvider)
        p.keys = {
            'message/media_0.db': {'enc_key': '00'},
            'message/message_0.db': {'enc_key': '00'},
            'message/biz_message_0.db': {'enc_key': '00'},
        }
        seen = []
        p._self_id_of_rel = lambda rel: seen.append(rel) or 1
        self.assertTrue(p._probe_self_id())
        self.assertEqual(seen, ['message/message_0.db'])

    def _first_name2id_sql(self, p):
        for q in p.queries:
            if 'Name2Id' in q:
                return q
        return None


class CountManyTests(unittest.TestCase):
    def test_counts_every_username(self):
        p = FakeLive(tempfile.mkdtemp(prefix='jev-live-'), {}, {})
        p._count = lambda u: len(u)
        self.assertEqual(p._count_many(['a', 'bb', 'ccc']), {'a': 1, 'bb': 2, 'ccc': 3})

    def test_empty_input_does_not_build_a_pool(self):
        p = FakeLive(tempfile.mkdtemp(prefix='jev-live-'), {}, {})
        self.assertEqual(p._count_many([]), {})


class ThumbnailIsolationTests(unittest.TestCase):
    def _provider(self, root):
        provider = LiveProvider.__new__(LiveProvider)
        provider.live_dir = Path(root) / 'db_storage'
        provider._thumb_cache = {}
        return provider

    def _thumb(self, root, username, name, mtime):
        folder = (Path(root) / 'msg' / 'attach'
                  / hashlib.md5(username.encode()).hexdigest() / '2026-09' / 'Img')
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(b'jpeg')
        os.utime(path, (mtime, mtime))
        return path

    def test_nearby_image_from_another_chat_is_never_selected(self):
        with tempfile.TemporaryDirectory() as root:
            provider = self._provider(root)
            own = self._thumb(root, 'chat-a', 'own_t_M.dat', 1000)
            self._thumb(root, 'chat-b', 'closer_t_M.dat', 1001)
            self.assertEqual(provider._thumb_for('chat-a', 1001), own)

    def test_missing_conversation_attachment_tree_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            provider = self._provider(root)
            self._thumb(root, 'chat-b', 'other_t_M.dat', 1000)
            self.assertIsNone(provider._thumb_for('chat-a', 1000))

    def test_thumbnail_cache_is_scoped_by_conversation(self):
        with tempfile.TemporaryDirectory() as root:
            provider = self._provider(root)
            a = self._thumb(root, 'chat-a', 'a_t_M.dat', 1000)
            b = self._thumb(root, 'chat-b', 'b_t_M.dat', 1000)
            self.assertEqual(provider._thumb_for('chat-a', 1000), a)
            self.assertEqual(provider._thumb_for('chat-b', 1000), b)



class SenderNameTests(unittest.TestCase):
    """群消息的说话人：群昵称优先，昵称本身是原始 id 时查 contact.db 换真名。

    实测踩到过：面板上展示原始 `@openim` 数字 id，判断 prompt 里也跟着不可读。
    """

    class FakeMessages(LiveProvider):
        def __init__(self, rows, contacts):
            self._rows = rows
            self._contact = contacts
            self.self_wxid = 'me'
            self.live_dir = Path('/nonexistent')
            self.keys = {}

        def _shards_for(self, username):
            return [('message/message_0.db', 'Msg_x')]

        def _self_id_of_rel(self, rel):
            return 1                      # 自己

        def _query(self, rel, sql):
            return self._rows

    @staticmethod
    def row(content):
        return {'create_time': 1700000000, 'ct': 0, 'real_sender_id': 2,
                'local_type': 1, 'content': content}

    def test_group_nickname_wins(self):
        p = self.FakeMessages([self.row('测试群友:\n好的')], {'me': ('', '我', 0)})
        self.assertEqual(p.messages('g@chatroom')[0]['name'], '测试群友')

    def test_raw_id_prefix_is_resolved_through_contacts(self):
        p = self.FakeMessages([self.row('12345678901234567@openim:\n训练完要发布')],
                              {'12345678901234567@openim': ('', '测试用户甲', 0)})
        self.assertEqual(p.messages('g@chatroom')[0]['name'], '测试用户甲')

    def test_unresolvable_id_is_left_alone(self):
        p = self.FakeMessages([self.row('wxid_unknown:\n在吗')], {})
        self.assertEqual(p.messages('g@chatroom')[0]['name'], 'wxid_unknown')

    def test_one_to_one_without_prefix_uses_the_chat_name(self):
        p = self.FakeMessages([self.row('晚上开会')], {'boss': ('张三', '', 0)})
        self.assertEqual(p.messages('boss')[0]['name'], '张三')

    def test_looks_like_raw_id(self):
        for raw in ('12345678901234567@openim', '12345@chatroom', 'wxid_abc', '987654321'):
            self.assertTrue(_looks_like_raw_id(raw), raw)
        for name in ('测试用户甲', '测试群友', 'tester', '测试用户甲@测试群', ''):
            self.assertFalse(_looks_like_raw_id(name), name)


class InterfaceParityTests(unittest.TestCase):
    """两个 Provider 在调用方眼里必须同形：history_ui 对两者传同一组参数。"""

    def test_both_providers_accept_with_counts(self):
        for cls in (LiveProvider, HistoryProvider):
            params = inspect.signature(cls.conversations).parameters
            self.assertIn('with_counts', params, f'{cls.__name__}.conversations')
            self.assertTrue(params['with_counts'].default is not inspect.Parameter.empty)

    def test_both_providers_offer_the_same_public_surface(self):
        for name in ('conversations', 'messages', 'display_name', 'category'):
            for cls in (LiveProvider, HistoryProvider):
                self.assertTrue(callable(getattr(cls, name, None)),
                                f'{cls.__name__}.{name}')


class SqlcipherResolutionTests(unittest.TestCase):
    def test_returns_a_usable_command(self):
        resolved = _resolve_sqlcipher()
        self.assertIsInstance(resolved, str)
        self.assertTrue(Path(resolved).exists() or resolved == 'sqlcipher')


if __name__ == '__main__':
    unittest.main()
