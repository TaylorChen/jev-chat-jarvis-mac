"""message_view 的离线回归：格式化是纯函数，窗口只负责画出来。

Run: python -B -m unittest discover -s tests
"""
import datetime
import sys
import tempfile
import unittest
from pathlib import Path

import AppKit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import message_view  # noqa: E402


class FormatRowsTests(unittest.TestCase):
    def test_rich_text_uses_an_nscolor_object_not_the_text_color_method(self):
        rich = message_view.attributed_rows([(0, 'them', None, 'hello')])
        color, _effective = rich.attribute_atIndex_effectiveRange_(
            AppKit.NSForegroundColorAttributeName, 0, None)
        self.assertIsInstance(color, AppKit.NSColor)
        self.assertFalse(callable(color))

    def test_image_message_builds_a_real_text_attachment(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'thumb.png'
            image = AppKit.NSImage.alloc().initWithSize_((8, 8))
            rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                None, 8, 8, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
            rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}) \
                .writeToFile_atomically_(str(path), True)
            rich = message_view.attributed_rows([(0, 'them', None, '[图片]', path)])
            self.assertTrue(rich.containsAttachments())

    def test_known_timestamp_is_formatted(self):
        ts = int(datetime.datetime(2026, 9, 23, 16, 31).timestamp())
        text = message_view.format_rows([(ts, 'them', '张三', '下午开会')])
        self.assertIn('09-23 16:31', text)
        self.assertIn('张三: 下午开会', text)

    def test_unknown_timestamp_does_not_crash(self):
        # OCR 消息没有时间戳（ts=0）：占位而不是崩，也不谎报时间
        self.assertIn('--:--', message_view.format_rows([(0, 'them', None, '某条')]))

    def test_my_messages_are_labelled_我(self):
        self.assertIn('我: 好的', message_view.format_rows([(0, 'me', None, '好的')]))

    def test_unknown_direction_is_said_out_loud(self):
        self.assertIn('方向未确认: 宽文本',
                      message_view.format_rows([(0, 'unknown', None, '宽文本')]))

    def test_group_sender_falls_back_to_对方(self):
        self.assertIn('对方: 在吗', message_view.format_rows([(0, 'them', None, '在吗')]))

    def test_multiline_body_stays_one_line(self):
        text = message_view.format_rows([(0, 'them', None, '第一行\n第二行')])
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn('第一行 ⏎ 第二行', text)

    def test_order_is_preserved(self):
        rows = [(0, 'them', None, '一'), (0, 'me', None, '二'), (0, 'them', None, '三')]
        lines = message_view.format_rows(rows).splitlines()
        self.assertEqual([line.split(': ')[-1] for line in lines], ['一', '二', '三'])


class FormatSummaryTests(unittest.TestCase):
    def test_counts_read_and_used(self):
        summary = message_view.format_summary('张三', [(0, 'them', None, 'x')] * 100, 100)
        self.assertIn('张三', summary)
        self.assertIn('读到 100 条', summary)
        self.assertIn('最近 99 条', summary)      # 当前这条不重复计入上下文

    def test_turn_defaults_are_reported_without_a_budget(self):
        summary = message_view.format_summary('', [(0, 'them', None, 'x')] * 6, 0)
        self.assertIn('（未识别）', summary)
        self.assertIn('读到 6 条', summary)

    def test_empty_list_does_not_produce_negative_counts(self):
        self.assertIn('最近 0 条', message_view.format_summary('', [], 0))


if __name__ == '__main__':
    unittest.main()
