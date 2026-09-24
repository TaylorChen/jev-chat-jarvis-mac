"""Offline regressions for #11. Run: python -B -m unittest discover -s tests -v.

Load the actual HUD methods through AST so the tests never start Cocoa, read the
screen, load user credentials, or make model calls. Perception uses synthetic OCR.
"""
import ast
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perception import Message, TextBlock, extract_messages


def hud_harness():
    tree = ast.parse((ROOT / 'src/hud.py').read_text())
    source = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HudController')
    names = {'_work_inner', '_push', '_reply_task', '_reply_current', '_push_reply',
             'applyReplyUpdate_', 'applyWaiting_', '_context_text', '_context_budget',
             '_context_for', '_stream_hook', '_message_rows', '_refresh_message_view',
             '_source_badge', 'applySource_', 'applyMessages_', 'applyWindow_',
             'applyTrigger_', '_window_text', '_trigger_text', '_render_meta',
             'pinSession_', '_recent_sessions', 'menuNeedsUpdate_', '_apply_pin',
             '_pin_rows', 'openSessions_',
             '_take_pregen', '_gen_with_pregen', '_finish_generate',
             '_prejudge_loop', '_pregen_loop'}
    methods = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for method in methods:
        method.decorator_list = []
    klass = ast.ClassDef(name='Harness', bases=[], keywords=[], body=methods, decorator_list=[])
    scope = {'fill': SimpleNamespace(locate_input=Mock(return_value={'box': None, 'rect': None, 'reason': 'test'})), 'time': time, 'threading': threading, '_log': lambda *_: None,
             'screen_capture_ok': lambda: True, 'read_conversation': Mock(),
             'PALETTE': {'muted': None}, 'CONTEXT_TURNS': 8, 'JUDGE_TURNS': 4,
             'userconfig': SimpleNamespace(get=lambda *_: ''),
             'SLOW_TICK': 1, 'BURST_TICK': .45, 'FAST_TICK': .25, 'BURST_READS': 3,
             'SETTLE_S': 1.2, 'STABLE_READS': 3, 'EARLY_SETTLE_S': .7, 'MIN_GAP_S': 2}
    module = ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[]))
    exec(compile(module, str(ROOT / 'src/hud.py'), 'exec'), scope)
    return scope['Harness'], scope


Harness, HUD = hud_harness()


def block(text, x, y, w, h=.035):
    return TextBlock(text, 1.0, x, y, w, h)


class OutgoingTests(unittest.TestCase):
    def setUp(self):
        self.h = h = Harness()
        for name, value in dict(
            _reply_key=None, _reply_epoch=0, _reply_worker=threading.local(),
            last_seen=None, analyzed_text=None, _win_wid=None, _fingerprint=None,
            _burst_left=3, _last_full=None, _show_boxes=False, _read_once=True,
            _gen_epoch=0, _last_skip_reason=None, _prejudge_req=None, _prejudge_result=None,
            _pregen_req=None, _pregen_result=None, _pregen_running=False,
            _prejudging=False, _paused=False, _analyzing=False, _db_mode=False,
            _stable_n=0, last_change_ts=0, last_analyze_ts=0,
            _prejudge_event=threading.Event(), _pregen_event=threading.Event(),
            slot_tones=['normal'], _stream_rows={}, _last_context=None,
        ).items():
            setattr(h, name, value)
        h._show = Mock()
        h._render = Mock()
        h._clear_candidates = Mock()
        h.rows = {'cand_header': Mock()}
        h._slot_active = lambda _: True
        h._payload_current = lambda _: True
        h.generator = Mock()
        h.judge = Mock()
        h._model_lock = threading.Lock()
        h._judged_once = True
        for name in ['applyIncoming_', 'applyPending_', 'applyJudgment_',
                     'applyCandidates_', 'applyStreamLine_', 'applyError_',
                     'applyPosition_', 'applyChat_', 'applyBoxes_']:
            setattr(h, name, Mock())
        self.queue = []
        self.logs = []
        HUD['_log'] = self.logs.append
        h.performSelectorOnMainThread_withObject_waitUntilDone_ = lambda s, p, w: self.queue.append((s, p))

    def read(self, blocks, title='chat'):
        messages = extract_messages(blocks)
        HUD['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': None,
            'window': {'wid': 1}, 'chat_title': title, 'messages': messages,
        }
        self.h._work_inner()
        return messages

    def flush(self):
        while self.queue:
            selector, payload = self.queue.pop(0)
            getattr(self.h, selector.replace(':', '_'))(payload)

    def incoming(self):
        self.read([block('下午开会', .40, .70, .15)])

    def test_only_own_short_message_never_enqueues_models(self):
        messages = self.read([block('11', .862, .284, .024, .024)])
        self.assertEqual(messages[0].side, 'me')
        self.assertIsNone(self.h._prejudge_req)
        self.assertIsNone(self.h._pregen_req)
        self.assertFalse(self.h._prejudge_event.is_set())
        self.assertFalse(self.h._pregen_event.is_set())
        self.flush()
        self.h.applyIncoming_.assert_not_called()
        self.h._clear_candidates.assert_called_once()

    def test_ambiguous_wide_message_is_not_incoming(self):
        messages = self.read([block('宽文本横跨左右分界', .38, .70, .55)])
        self.assertEqual(messages[0].side, 'unknown')
        self.assertIsNone(self.h._reply_key)
        self.assertIsNone(self.h._pregen_req)

    def test_shifted_outgoing_continuation_stays_with_bubble(self):
        messages = self.read([block('自己长消息第一行', .55, .70, .29),
                              block('较短续行', .535, .66, .12)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'me')
        self.assertAlmostEqual(messages[0].x + messages[0].w, .84)
        self.assertIsNone(self.h._prejudge_req)

    def test_cropped_central_continuation_is_unknown(self):
        messages = self.read([block('只剩续行', .535, .66, .12)])
        self.assertEqual(messages[0].side, 'unknown')
        self.assertIsNone(self.h._pregen_req)

    def test_later_wide_line_can_resolve_initial_unknown(self):
        messages = self.read([block('短首行', .535, .70, .12),
                              block('后面更长的一行', .55, .66, .29),
                              block('第三行', .54, .62, .13)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'me')
        self.assertEqual(len(messages[0].lines), 3)
        self.assertIsNone(self.h._pregen_req)

    def test_opposite_known_sides_never_fold_despite_close_edges(self):
        messages = extract_messages([block('收到的消息', .495, .70, .18),
                                     block('自己发出的消息', .51, .66, .34)])
        self.assertEqual([m.side for m in messages], ['them', 'me'])

    def test_viewer_refresh_is_marshalled_to_the_main_thread(self):
        # AppKit 只能在主线程碰：读线程里直接 reload 会把整个应用卡死（真实故障）
        h = Harness()
        pushed = []
        h._push = lambda sel, payload=None: pushed.append(sel)
        h.message_viewer = Mock()
        h._refresh_message_view()
        self.assertEqual(pushed, ['applyMessages:'])
        h.message_viewer.reload.assert_not_called()

    def test_no_viewer_no_push(self):
        h = Harness()
        pushed = []
        h._push = lambda sel, payload=None: pushed.append(sel)
        h._refresh_message_view()
        self.assertEqual(pushed, [])

    def test_apply_messages_reloads_only_while_visible(self):
        h = Harness()
        viewer = Mock()
        viewer.window.isVisible.return_value = True
        h.message_viewer = viewer
        h._db_mode = False          # 预算走按轮次默认，_context_budget 要读它
        h._last_msgs = [Message(text='下午开会', side='them', y=0.0, conf=1.0, lines=[])]
        h._last_chat = '张三'
        h.applyMessages_(None)
        viewer.reload.assert_called_once()
        viewer.reset_mock()
        viewer.window.isVisible.return_value = False
        h.applyMessages_(None)
        viewer.reload.assert_not_called()

    def test_ocr_mode_still_tries_the_visual_fallback(self):
        # 读屏模式没有这个兜底时，没给辅助功能权限的机器「填入」就直接不可用
        import input_region
        with patch.object(input_region, 'locate_visual_input', return_value=None) as vis:
            self.read([block('下午开会', .40, .70, .15)])
        vis.assert_called_once()

    def test_real_incoming_still_triggers_both_jobs_and_keeps_own_context(self):
        self.read([block('下午开会', .40, .70, .15), block('我会带材料', .78, .50, .10)])
        self.assertEqual(self.h._prejudge_req[0], '下午开会')
        self.assertEqual(self.h._pregen_req[0], '下午开会')
        self.assertIn('我: 我会带材料', self.h._prejudge_req[1])
        self.flush()
        self.h.applyIncoming_.assert_called_once()

    def test_incoming_wrapped_message_and_sender_preserved(self):
        messages = extract_messages([block('小王', .40, .80, .05, .020),
                                     block('第一行正文', .40, .65, .25),
                                     block('续行正文', .405, .61, .12)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'them')
        self.assertEqual(messages[0].sender, '小王')
        self.assertEqual(len(messages[0].lines), 2)

    def test_no_incoming_clears_jobs_and_pending_ui(self):
        self.incoming()
        old_epoch = self.h._reply_epoch
        for selector in ('applyJudgment:', 'applyCandidates:', 'applyStreamLine:'):
            self.h._push_reply(selector, 'late result', old_epoch)
        self.read([block('自己回复', .80, .50, .10)])
        self.flush()
        for name in ('applyIncoming_', 'applyJudgment_', 'applyCandidates_', 'applyStreamLine_'):
            getattr(self.h, name).assert_not_called()
        self.assertIsNone(self.h.last_seen)
        self.assertIsNone(self.h.analyzed_text)
        self.assertIsNone(self.h._prejudge_req)
        self.assertIsNone(self.h._pregen_req)

    def test_empty_ocr_also_invalidates_target(self):
        self.incoming()
        self.read([])
        self.assertIsNone(self.h._reply_key)
        self.assertIsNone(self.h._pregen_req)

    def test_old_worker_completion_and_same_text_reappearance(self):
        self.incoming()
        epoch = self.h._reply_epoch
        self.read([])
        self.incoming()
        self.flush()
        self.h._reply_task(epoch, self.h._push, 'applyCandidates:', 'old replies')
        self.flush()
        self.h.applyCandidates_.assert_not_called()

    def test_stream_callback_retains_original_epoch(self):
        self.incoming()
        callback = self.h._reply_task(self.h._reply_epoch, self.h._stream_hook, time.perf_counter())
        self.read([])
        self.incoming()
        callback(0, 'normal', 'old reply')
        self.flush()
        self.h.applyStreamLine_.assert_not_called()

    def test_old_generation_does_not_restart_or_rank(self):
        self.incoming()
        epoch = self.h._reply_epoch
        self.read([])
        self.h._reply_task(epoch, self.h._gen_with_pregen, '下午开会', None)
        self.h.generator.generate.assert_not_called()
        self.h._reply_task(epoch, self.h._finish_generate, {}, None, time.perf_counter(), None)
        self.h.judge.rank_candidates.assert_not_called()

    def test_same_text_in_different_chat_changes_epoch(self):
        self.incoming()
        epoch = self.h._reply_epoch
        self.read([block('下午开会', .40, .70, .15)], title='another chat')
        self.assertGreater(self.h._reply_epoch, epoch)

    def test_prejudge_completion_cannot_repopulate_cleared_state(self):
        self.incoming()
        class Finished(BaseException):
            pass
        self.h._prejudge_event = Mock()
        self.h._prejudge_event.wait.side_effect = [None, Finished()]
        def judge_then_clear(*args, **kwargs):
            self.read([])
            return {'intent': '约会议', 'confidence': 1, 'risk': 1}
        self.h.judge.judge.side_effect = judge_then_clear
        with self.assertRaises(Finished):
            self.h._prejudge_loop()
        self.assertIsNone(self.h._prejudge_result)

    def test_pregen_completion_cannot_repopulate_cleared_state(self):
        self.incoming()
        class Finished(BaseException):
            pass
        self.h._pregen_event = Mock()
        self.h._pregen_event.wait.side_effect = [None, Finished()]
        def generate_then_clear(*args, **kwargs):
            self.read([])
            return {'groups': []}
        self.h.generator.generate.side_effect = generate_then_clear
        with self.assertRaises(Finished):
            self.h._pregen_loop()
        self.assertIsNone(self.h._pregen_result)

    def test_prejudge_worker_logs_unexpected_errors_and_keeps_running(self):
        class Finished(BaseException):
            pass
        self.h._prejudge_req = ('broken',)
        self.h._prejudge_event = Mock()
        self.h._prejudge_event.wait.side_effect = [None, Finished()]
        with self.assertRaises(Finished):
            self.h._prejudge_loop()
        self.assertTrue(any('预判 worker 异常' in line for line in self.logs))
        self.assertFalse(self.h._prejudging)

    def test_pregen_worker_logs_unexpected_errors_and_clears_running_flag(self):
        class Finished(BaseException):
            pass
        self.h._pregen_req = ('broken',)
        self.h._pregen_event = Mock()
        self.h._pregen_event.wait.side_effect = [None, Finished()]
        self.h._pregen_running = True
        with self.assertRaises(Finished):
            self.h._pregen_loop()
        self.assertTrue(any('预生成 worker 异常' in line for line in self.logs))
        self.assertFalse(self.h._pregen_running)


class DbModeTests(unittest.TestCase):
    """JEV_DB_MODE=1：消息来自 live 加密库，测试替掉读取器本身。

    原 OCR 写入路径（read_conversation）必须完全不被触碰，否则「不读屏」的
    前提就不成立。
    """

    def setUp(self):
        self.h = h = Harness()
        for name, value in dict(
            _win_wid=None, _fingerprint=None, _burst_left=3, _last_full=None,
            _show_boxes=False, _read_once=False, _db_mode=True, _db_read_count=0,
            _reply_key=None, _reply_epoch=0, _reply_worker=threading.local(),
            _gen_epoch=0, _stable_n=0, last_seen=None, analyzed_text=None,
            last_change_ts=0, last_analyze_ts=0, _last_skip_reason=None,
            _prejudge_req=None, _prejudge_result=None, _pregen_req=None,
            _pregen_result=None, _prejudging=False, _paused=False, _analyzing=False,
            _prejudge_event=threading.Event(), _pregen_event=threading.Event(),
            slot_tones=['normal'], _last_context=None, _input_next=0,
        ).items():
            setattr(h, name, value)
        h.rows = {'cand_header': Mock()}
        h._show = Mock()
        h._render = Mock()
        h._clear_candidates = Mock()
        h._db_reader = Mock()
        for name in ('applyIncoming_', 'applyPending_', 'applyJudgment_',
                     'applyCandidates_', 'applyStreamLine_', 'applyError_',
                     'applyPosition_', 'applyChat_', 'applyBoxes_', 'applyHidden_',
                     'applyWaiting_'):
            setattr(h, name, Mock())
        self.queue = []
        self.logs = []
        HUD['_log'] = self.logs.append
        h.performSelectorOnMainThread_withObject_waitUntilDone_ = \
            lambda s, p, w: self.queue.append((s, p))

    def flush(self):
        while self.queue:
            selector, payload = self.queue.pop(0)
            getattr(self.h, selector.replace(':', '_'))(payload)

    def db_read(self, messages, title='张三', window=None):
        """一份 DBReader.read_conversation 形状的结果（含合成窗口）。"""
        HUD['read_conversation'].reset_mock()
        HUD['fill'].locate_input.reset_mock()
        self.h._db_reader.read_conversation.return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': b'db:1',
            'window': window or {'wid': 0, 'pid': 0, 'title': '',
                                 'x': 200.0, 'y': 200.0, 'w': 800.0, 'h': 600.0,
                                 'synthetic': True},
            'chat_title': title, 'messages': messages, 'timing_ms': {'db': 42},
            'error': None,
        }
        self.h._work_inner()

    @staticmethod
    def msg(text, side, sender=None):
        return Message(text=text, side=side, y=0.0, conf=1.0, sender=sender,
                       lines=[text])

    def test_db_mode_never_touches_ocr_path(self):
        self.db_read([self.msg('下午开会', 'them', '张三')])
        HUD['read_conversation'].assert_not_called()
        self.h._db_reader.read_conversation.assert_called_once()
        self.assertEqual(self.h._db_read_count, 1)

    def test_db_logs_never_contain_the_real_chat_title(self):
        sentinel = 'PRIVATE_CUSTOMER_GROUP_9381'
        self.db_read([self.msg('下午开会', 'them', '张三')], title=sentinel)
        self.assertNotIn(sentinel, '\n'.join(self.logs))

    def test_synthetic_window_skips_ax_and_screen_capture(self):
        # 列不到微信窗口时不该去截屏做视觉兜底：DB 模式本就不该要录屏权限
        self.db_read([self.msg('下午开会', 'them', '张三')])
        HUD['fill'].locate_input.assert_not_called()
        self.assertIsNone(self.h._input_target['box'])
        self.assertIn('数据库模式', self.h._input_target['reason'])

    def test_db_mode_never_captures_the_screen_for_input_targeting(self):
        import input_region
        import visual_fill
        window = {'wid': 1, 'pid': 1, 'title': '微信',
                  'x': 0.0, 'y': 0.0, 'w': 800.0, 'h': 600.0}
        with patch.object(input_region, 'locate_visual_input') as vis, \
                patch.object(visual_fill, 'chat_signature') as sig:
            self.db_read([self.msg('下午开会', 'them', '张三')], window=window)
        HUD['fill'].locate_input.assert_called_once()     # AX 定位照做
        vis.assert_not_called()                          # 不截屏
        sig.assert_not_called()

    def test_db_history_feeds_both_halves_as_real_context(self):
        self.db_read([self.msg('材料我准备好了', 'me'),
                      self.msg('下午开会', 'them', '张三')])
        text, context, sender, prev, _epoch = self.h._prejudge_req
        self.assertEqual(text, '下午开会')
        self.assertEqual(sender, '张三')
        self.assertIn('我: 材料我准备好了', context)
        self.assertEqual(self.h._pregen_req[0], '下午开会')
        self.assertTrue(self.h._prejudge_event.is_set())
        self.assertTrue(self.h._pregen_event.is_set())

    def test_db_read_failure_reports_and_backs_off(self):
        self.h._db_reader.read_conversation.side_effect = RuntimeError('库被锁')
        self.h._work_inner()
        self.flush()
        self.h.applyError_.assert_called_once()
        self.assertIn('DB 读取失败', self.h.applyError_.call_args[0][0])
        self.assertIsNone(self.h._pregen_req)

    def test_db_idle_read_does_not_enqueue_models(self):
        self.h._db_reader.read_conversation.return_value = {
            'ok': True, 'unchanged': True, 'fingerprint': b'db:idle:0',
            'window': {'wid': 0, 'pid': 0, 'x': 200.0, 'y': 200.0,
                       'w': 800.0, 'h': 600.0, 'synthetic': True},
            'chat_title': None, 'messages': [], 'timing_ms': {'db': 0}, 'error': None}
        self.h._work_inner()
        self.flush()
        self.assertIsNone(self.h._prejudge_req)
        self.assertIsNone(self.h._pregen_req)
        self.h.applyWaiting_.assert_called()


class LogFilePrivacyTests(unittest.TestCase):
    def test_log_file_is_created_and_repaired_as_0600(self):
        tree = ast.parse((ROOT / 'src/hud.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_log')
        module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'jev.log'
            path.write_text('old\n')
            path.chmod(0o644)
            scope = {'time': time, 'os': os, 'sys': sys, 'LOG_PATH': path}
            exec(compile(module, str(ROOT / 'src/hud.py'), 'exec'), scope)
            scope['_log']('safe')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

class ContextDepthTests(unittest.TestCase):
    """判断/生成各拿多少上下文：数据库直读默认用读到的全部历史（用户要的「基于近 100 条」）。"""

    def _harness(self, db_mode):
        h = Harness()
        h._db_mode = db_mode
        return h

    @staticmethod
    def msgs(n, me=0):
        out = [Message(text=f'消息{i}', side='them', y=0.0, conf=1.0, lines=[]) for i in range(n)]
        for i in range(me):
            out[i] = Message(text=f'我说的{i}', side='me', y=0.0, conf=1.0, lines=[])
        return out

    def test_db_mode_uses_the_whole_read_history(self):
        h = self._harness(db_mode=True)
        msgs = self.msgs(100)
        self.assertEqual(h._context_budget(msgs), 100)
        context = h._context_for(msgs, msgs[-1], judge=True)
        # 判断半边也拿到全部历史（不再被 JUDGE_TURNS=4 截断），且不含被判断的那条
        self.assertEqual(len(context.splitlines()), 99)
        self.assertIn('消息0', context)
        self.assertNotIn('消息99', context)

    def test_ocr_mode_keeps_the_turn_defaults(self):
        h = self._harness(db_mode=False)
        msgs = self.msgs(20)
        self.assertEqual(h._context_budget(msgs), 0)
        self.assertEqual(len(h._context_for(msgs, msgs[-1], judge=True).splitlines()), 4)
        self.assertEqual(len(h._context_for(msgs, msgs[-1], judge=False).splitlines()), 8)

    def test_explicit_message_budget_wins(self):
        h = self._harness(db_mode=False)
        msgs = self.msgs(30)
        with patch.dict(HUD, {'userconfig': SimpleNamespace(
                get=lambda name, *_: '12' if name == 'JEV_CONTEXT_MESSAGES' else '')}):
            self.assertEqual(h._context_budget(msgs), 12)
            self.assertEqual(len(h._context_for(msgs, msgs[-1], judge=True).splitlines()), 12)

    def test_legacy_db_history_switch_still_means_the_same_thing(self):
        # 用户 env 里还留着 JEV_DB_HISTORY=1（早期实验开关，代码曾被删掉）：继续认它，
        # 语义就是「拿本地库的历史当上下文」，别让它变成一行没人读的死配置
        h = self._harness(db_mode=False)
        msgs = self.msgs(15)
        with patch.dict(HUD, {'userconfig': SimpleNamespace(
                get=lambda name, *_: '1' if name == 'JEV_DB_HISTORY' else '')}):
            self.assertEqual(h._context_budget(msgs), 15)

    def test_budget_is_capped(self):
        h = self._harness(db_mode=True)
        with patch.dict(HUD, {'userconfig': SimpleNamespace(
                get=lambda name, *_: '99999' if name == 'JEV_CONTEXT_MESSAGES' else '')}):
            self.assertEqual(h._context_budget(self.msgs(9999)), 400)

    def test_db_mode_request_carries_the_long_context(self):
        h = self._harness(db_mode=True)
        h._prejudge_event = threading.Event()
        h._pregen_event = threading.Event()
        h.slot_tones = ['normal']
        h._reply_epoch = 0
        msgs = self.msgs(12)
        context = h._context_for(msgs, msgs[-1], judge=True)
        self.assertIn('消息0', context)          # 第 1 条也在，说明没被截成 4 条
        self.assertEqual(len(context.splitlines()), 11)


class MessageRowsTests(unittest.TestCase):
    """「查看读到的消息…」的数据整理：悬浮窗用列表，窗口只管画。"""

    def test_rows_keep_time_side_sender_text(self):
        h = Harness()
        h._last_msgs = [Message(text='下午开会', side='them', y=0.0, conf=1.0,
                                sender='张三', lines=[], ts=1700000000),
                        Message(text='好的', side='me', y=0.0, conf=1.0, lines=[], ts=0)]
        self.assertEqual(h._message_rows(),
                         [(1700000000, 'them', '张三', '下午开会', None), (0, 'me', None, '好的', None)])

    def test_missing_last_read_is_empty_not_an_error(self):
        self.assertEqual(Harness()._message_rows(), [])


class WindowAndTriggerTextTests(unittest.TestCase):
    """面板两行元信息：历史窗口的起止时间 + JEV 何时被触发（用户要求在面板上可见）。"""

    @staticmethod
    def msg(ts):
        return Message(text='x', side='them', y=0.0, conf=1.0, lines=[], ts=ts)

    def test_same_day_window_shows_start_and_end(self):
        h = Harness()
        h._last_msgs = [self.msg(1758600000), self.msg(1758610800)]     # 相隔 3 小时
        text = h._window_text()
        self.assertRegex(text, r'^历史 \d\d-\d\d \d\d:\d\d → \d\d:\d\d · 2 条$')

    def test_cross_day_window_shows_both_dates(self):
        h = Harness()
        h._last_msgs = [self.msg(1758520000), self.msg(1758620000)]     # 跨天
        text = h._window_text()
        self.assertEqual(text.count('-'), 2)          # 两个日期
        self.assertIn('→', text)

    def test_ocr_messages_without_timestamps_are_reported_honestly(self):
        h = Harness()
        h._last_msgs = [self.msg(0), self.msg(0)]
        self.assertEqual(h._window_text(), '屏幕可见 2 条 · 读屏无时间戳')

    def test_no_messages_means_no_line(self):
        self.assertEqual(Harness()._window_text(), '')

    def test_trigger_text_phases(self):
        h = Harness()
        self.assertIn('已触发', h._trigger_text('fired'))
        self.assertIn('判定完成', h._trigger_text('done', ' · 603ms'))
        self.assertIn('603ms', h._trigger_text('done', ' · 603ms'))
        self.assertIn('判定中', h._trigger_text('pending'))
        self.assertIn('等待对方消息', h._trigger_text())
        self.assertIn('停稳 1.2s', h._trigger_text('fired'))   # SETTLE_S 取自夹具 scope


class PinSessionTests(unittest.TestCase):
    """「跟随会话」：微信不记录「当前打开的会话」，所以要有手动钉住这条路。"""

    def _h(self, db_mode=True):
        h = Harness()
        h._db_mode = db_mode
        h._last_msgs = []
        h._pin_label = ''
        h._push = Mock()
        h._render = Mock()
        h._next_read_ts = 10
        h._fingerprint = b'old'
        h.last_seen = 'old'
        h._db_reader = SimpleNamespace(
            pin=None, set_pin=Mock(),
            provider=SimpleNamespace(display_name=lambda u: {'boss_a': '张三'}.get(u, u)))
        return h

    class _Sender:
        def __init__(self, value):
            self._v = value

        def representedObject(self):
            return self._v

    def test_pin_sets_reader_name_and_badge(self):
        h = self._h()
        h.pinSession_(self._Sender('boss_a'))
        h._db_reader.set_pin.assert_called_once_with('boss_a')
        self.assertEqual(h._pin_label, '张三')
        self.assertIn('手动', h._source_badge())
        h._push.assert_called_with('applySource:', h._source_badge())

    def test_pin_forces_an_immediate_rerun(self):
        # 钉住后必须马上重读：否则要等下一个 tick，用户会以为没生效
        h = self._h()
        h.pinSession_(self._Sender('boss_a'))
        self.assertEqual(h._next_read_ts, 0)
        self.assertIsNone(h._fingerprint)
        self.assertIsNone(h.last_seen)

    def test_unpin_clears_the_manual_marker(self):
        h = self._h()
        h.pinSession_(self._Sender(None))
        h._db_reader.set_pin.assert_called_once_with(None)
        self.assertEqual(h._pin_label, '')
        self.assertNotIn('手动', h._source_badge())

    def test_ocr_mode_explains_it_needs_the_database(self):
        h = self._h(db_mode=False)
        h._db_reader = None
        h.pinSession_(self._Sender('boss_a'))
        h._render.assert_called()

    def test_recent_sessions_skips_official_accounts_and_shows_unread(self):
        h = Harness()
        rows = [{'username': 'g1', 'u': 3}, {'username': 'gh_x', 'u': 0}, {'username': 'p1', 'u': 0}]
        names = {'g1': '刷屏群', 'p1': '张三', 'gh_x': '某公众号'}
        h._db_reader = SimpleNamespace(provider=SimpleNamespace(
            _query=lambda rel, sql: rows,
            category=lambda u: '公众号' if u.startswith('gh_') else '个人',
            display_name=lambda u: names[u]))
        self.assertEqual(h._recent_sessions(5),
                         [('g1', '刷屏群（3 条未读）'), ('p1', '张三')])

    def test_recent_sessions_query_failure_is_logged_not_silently_empty(self):
        h = Harness()
        logs = []
        HUD['_log'] = logs.append
        h._db_reader = SimpleNamespace(provider=SimpleNamespace(
            _query=Mock(side_effect=RuntimeError('read failed'))))
        self.assertEqual(h._recent_sessions(5), [])
        self.assertTrue(any('会话列表读取失败' in line for line in logs))

    def test_pin_rows_start_with_auto(self):
        h = self._h()
        h._recent_sessions = lambda n: [('boss_a', '张三（2 条未读）')]
        rows = h._pin_rows()
        self.assertEqual(rows[0][0], '')
        self.assertIn('自动', rows[0][1])
        self.assertEqual(rows[1], ('boss_a', '张三（2 条未读）'))

    def test_panel_button_and_menu_share_the_same_pin_logic(self):
        # 面板按钮选中一行 → 与菜单点一项完全同一条路径
        h = self._h()
        h._apply_pin('boss_a')
        h._db_reader.set_pin.assert_called_once_with('boss_a')
        self.assertEqual(h._pin_label, '张三')

    def test_pin_rows_without_reader_is_just_auto(self):
        h = Harness()
        h._db_reader = None
        self.assertEqual([r[0] for r in h._pin_rows()], [''])

    def test_no_reader_no_sessions(self):
        self.assertEqual(Harness()._recent_sessions(5), [])


class SourceBadgeTests(unittest.TestCase):
    """面板标题必须一直写清「在读库还是读屏」+ 条数（用户反复问的那件事）。"""

    def test_db_mode_reports_mode_and_count(self):
        h = Harness()
        h._db_mode = True
        h._last_msgs = [object()] * 99
        self.assertEqual(h._source_badge(), 'jev-jarvis · 数据库直读 99 条')

    def test_db_mode_before_the_first_read_has_no_count(self):
        h = Harness()
        h._db_mode = True
        self.assertEqual(h._source_badge(), 'jev-jarvis · 数据库直读')

    def test_ocr_mode_says_so(self):
        h = Harness()
        h._db_mode = False
        self.assertEqual(h._source_badge(), 'jev-jarvis · OCR 读屏')


if __name__ == '__main__':
    unittest.main()
