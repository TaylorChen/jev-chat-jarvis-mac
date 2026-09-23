#!/usr/bin/env python3
"""history.py — 本地会话历史上下文提供者（数据源：wcdb-key-tool 解密库）

给 judge/generate 的 context 参数供料。从微信窗口标题（或 OCR 消息文本）
定位会话，取最近 N 条文本消息格式化为「我/对方」对话串。

同名消歧（备注/群名撞名）三级策略：
  L1 内容反查：OCR 触发消息文本在候选会话最近消息中做包含匹配——
     屏幕上正开着的会话必然包含它，同名候选只有真的那个命中。
  L2 近期活跃：L1 失配（OCR 错字/图片消息）时按 SessionTable.sort_timestamp
     取最近活跃候选。
  L3 优雅降级：仍无法唯一确定 → 返回 None，调用方按无上下文继续。

数据格式（微信 4.x macOS）：
  message_*.db: Msg_<md5(username)> 表；WCDB_CT_message_content=4 为 zstd 压缩；
  群文本消息 message_content 形如「昵称:\\n内容」；real_sender_id 联
  message 库 Name2Id.rowid 得 username；自己 = self_wxid。

用法（CLI 冒烟）：
  uv run python src/history.py --title "会话名" [--text "当前消息"] [--n 10]
  uv run python src/history.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path

sys_path = str(Path(__file__).parent)
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

import wechat_keys  # noqa: E402

try:
    import zstandard as _zstd
    import threading as _threading
    _dctx_tls = _threading.local()

    def _get_dctx():
        """ZstdDecompressor 非线程安全，每线程独立实例（并发读多库时必需）。"""
        dctx = getattr(_dctx_tls, "dctx", None)
        if dctx is None:
            dctx = _zstd.ZstdDecompressor()
            _dctx_tls.dctx = dctx
        return dctx
except ImportError:   # 压缩行直接跳过，不影响主体功能
    _dctx_tls = None

    def _get_dctx():
        return None

DEFAULT_DB_DIR = wechat_keys.decrypted_dir()
MSG_TYPES_TEXT = 1
SCAN_RECENT = 300          # L1 内容反查时每候选扫的最近行数
MAX_CONTEXT_CHARS = 1400


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        # check_same_thread=False：连接在主线程创建，供 UI 工作线程只读复用
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    except sqlite3.Error:
        return None


def _decompress(content) -> str:
    """WCDB_CT=4 的行是 zstd 帧（28 b5 2f fd 开头）；失败返回空串。"""
    if isinstance(content, bytes):
        dctx = _get_dctx()
        if dctx is None:
            return ""
        try:
            return dctx.decompress(content).decode("utf-8", "replace")
        except Exception:
            return ""
    return content or ""


def _strip_sender_prefix(text: str) -> tuple[str, str]:
    """群消息形如「昵称:\\n内容」→ (显示名, 正文)；无前缀 → ("", 原文)。"""
    head, sep, rest = text.partition(":\n")
    if sep and head and "\n" not in head and len(head) <= 64:
        return head, rest
    return "", text


class HistoryProvider:
    def __init__(self, db_dir: Path | str | None = None,
                 self_wxid: str | None = None):
        self.db_dir = Path(db_dir) if db_dir else DEFAULT_DB_DIR
        if not self_wxid:
            # 解密目录名不含 wxid 时，退回从微信 live 目录名推断（同机同账号）
            try:
                from live_db import find_live_dir
                import re as _re
                live = find_live_dir()
                if live:
                    m = _re.match(r"(.+)_\d{1,4}$", live.parent.name)
                    self_wxid = m.group(1) if m else None
            except Exception:
                pass
        self.self_wxid = self_wxid
        self.self_wxid = self_wxid
        self._contact = _ro(self.db_dir / "contact/contact.db")
        self._session = _ro(self.db_dir / "session/session.db")
        self._msg_dbs: list[sqlite3.Connection] = []
        for name in sorted(self.db_dir.glob("message/*.db")):
            # 含 message_*.db 与 biz_message_*.db（公众号，同样 Msg_<md5(username)> 命名）
            conn = _ro(name)
            if conn:
                self._msg_dbs.append(conn)
        self._name2id: dict[str, int] = {}
        self._name2id_by_db: dict = {}   # Name2Id 是每库独立的，rowid 不能跨库合并
        self._verify_flag: dict[str, int] = {}
        self._table_cache: dict[str, tuple] = {}   # username -> ("Msg_xxx", db)
        self._load_name2id()
        self._load_verify_flags()

    def close(self):
        for c in (self._contact, self._session, *self._msg_dbs):
            if c:
                c.close()

    # ---------- 基础映射 ----------

    def _load_name2id(self):
        for db in self._msg_dbs:
            try:
                rows = db.execute("SELECT user_name, rowid FROM Name2Id").fetchall()
            except sqlite3.Error:
                continue
            self._name2id_by_db[db] = {u: r for u, r in rows}
            self._name2id.update(self._name2id_by_db[db])   # 合并版仅供展示回退

    def _self_id_in(self, db) -> int | None:
        """自己在某个消息库里的 sender_id（每库独立，不能跨库查）。"""
        return (self._name2id_by_db.get(db, {}).get(self.self_wxid)
                if self.self_wxid else None)

    def _tables_for(self, username: str) -> list[tuple[str, sqlite3.Connection]]:
        """username → 该会话在所有消息库中的 (Msg_<md5> 表名, 连接) 列表。

        微信 4.x 把会话按时间分片存进 message_0/1/2…（同名表），完整会话 =
        各库并集；公众号表则在 biz_message_*。同名表在多库同时存在是常态。
        """
        if username not in self._table_cache:
            table = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
            found = []
            for db in self._msg_dbs:
                hit = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()
                if hit:
                    found.append((table, db))
            self._table_cache[username] = found
        return self._table_cache[username]

    # ---------- 会话解析（同名消歧在这里） ----------

    def resolve(self, title: str = "", current_text: str = "") -> str | None:
        """输入：窗口标题（备注/昵称/群名）+ OCR 触发消息文本；输出 username 或 None。

        同名时按 L1(内容反查) → L2(近期活跃) → L3(放弃) 消歧。
        title 查无候选时，退化为「最近活跃会话 + 内容反查」。
        """
        candidates = self._candidates_by_title(title) if title else []
        if not candidates:
            # 无标题回退：只能靠内容反查，反查不中就是定位不到，不拿最近会话凑数
            if current_text:
                return self._filter_by_text(self._recent_sessions(15), current_text)
            return None
        if len(candidates) > 1 and current_text:
            hit = self._filter_by_text(candidates, current_text)
            if hit:
                return hit
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # L2：同名候选无文本可反查时，仅当第一候选明显比第二活跃（>24h）才采信
            ts = self._recency_of(candidates[:2])
            if ts[0] and ts[1] and ts[0] - ts[1] > 86400:
                return candidates[0]
        return None  # L3：宁可没有上下文，不挂错会话

    def _candidates_by_title(self, title: str) -> list[str]:
        """显示名 → username 候选（按近期活跃排序）。撞名就返回多个。"""
        if not self._contact:
            return []
        t = title.strip()
        # 群名在窗口标题可能带成员数后缀「xxx (25)」
        stripped = t.rsplit(" (", 1)[0].strip()
        likes = [t] + ([stripped] if stripped and stripped != t else [])
        usernames: set[str] = set()
        for cand in likes:
            rows = self._contact.execute(
                "SELECT username FROM contact WHERE remark=? OR nick_name=? OR alias=?",
                (cand, cand, cand)).fetchall()
            usernames.update(u for (u,) in rows if u)
        return self._sort_by_recency(usernames)

    def _recent_sessions(self, k: int) -> list[str]:
        if not self._session:
            return []
        try:
            rows = self._session.execute(
                "SELECT username FROM SessionTable ORDER BY sort_timestamp DESC LIMIT ?",
                (k,)).fetchall()
        except sqlite3.Error:
            return []
        return [u for (u,) in rows if u]

    def _recency_of(self, usernames: list[str]) -> list[int | None]:
        """查 SessionTable.sort_timestamp，查不到返回 None。"""
        if not self._session:
            return [None] * len(usernames)
        out = []
        for u in usernames:
            row = self._session.execute(
                "SELECT sort_timestamp FROM SessionTable WHERE username=?", (u,)).fetchone()
            out.append(row[0] if row else None)
        return out

    def _sort_by_recency(self, usernames: set[str]) -> list[str]:
        if not self._session or not usernames:
            return list(usernames)
        qmarks = ",".join("?" * len(usernames))
        try:
            rows = self._session.execute(
                f"SELECT username, sort_timestamp FROM SessionTable "
                f"WHERE username IN ({qmarks}) ORDER BY sort_timestamp DESC",
                tuple(usernames)).fetchall()
        except sqlite3.Error:
            return list(usernames)
        found = [u for (u, _) in rows]
        found += [u for u in usernames if u not in set(found)]  # 不在会话表的排最后
        return found

    def _filter_by_text(self, candidates: list[str], text: str) -> str | None:
        """L1：触发消息文本是否出现在候选会话的最近消息里。"""
        if not text or len(text.strip()) < 4:   # 太短的内容反查不可靠
            return None
        needle = "".join(text.split())          # 去空白再比，抗 OCR 换行差异
        for username in candidates:
            for content, _mine in self._recent_rows(username, SCAN_RECENT):
                hay = "".join(_strip_sender_prefix(content)[1].split())
                if needle in hay:
                    return username
        return None

    def _recent_rows(self, username: str, limit: int) -> list[tuple[str, bool]]:
        """跨库取最新 limit 条文本，按时间降序去重，返回 (内容, 是否我发送)。"""
        merged, seen = [], set()
        for table, db in self._tables_for(username):
            self_id = self._self_id_in(db)
            try:
                rows = db.execute(
                    f"SELECT sort_seq, create_time, message_content, "
                    f"WCDB_CT_message_content, real_sender_id FROM {table} "
                    f"WHERE local_type={MSG_TYPES_TEXT} "
                    f"AND (create_time IS NULL OR create_time > 0) "
                    f"ORDER BY sort_seq DESC LIMIT ?", (limit,)).fetchall()
            except sqlite3.Error:
                continue
            for sort_seq, ts, content, ct, sender in rows:
                if ct:
                    content = _decompress(content)
                if not content:
                    continue
                key = (ts, sender, hash(content))
                if key in seen:   # 分片重叠/迁移行去重
                    continue
                seen.add(key)
                merged.append((ts or 0, sort_seq or 0, content,
                               self_id is not None and sender == self_id))
        merged.sort(key=lambda r: (r[0], r[1]), reverse=True)
        return [(c, mine) for _, _, c, mine in merged[:limit]]

    # ---------- 上下文格式化 ----------

    # ---------- 历史分析供料（history_analyze 用） ----------

    def _load_verify_flags(self):
        """contact.verify_flag：bit0x8 为官方认证号（公众号可能是 wxid 格式用户名）。"""
        if not self._contact:
            return
        try:
            for u, v in self._contact.execute(
                    "SELECT username, verify_flag FROM contact"):
                self._verify_flag[u] = v or 0
        except sqlite3.Error:
            pass

    def category(self, username: str) -> str:
        """公众号（gh_ 前缀 / 官方聚合会话 / 认证账号）/ 群聊（@chatroom）/ 个人。"""
        if (username.startswith("gh_") or username in (
                "brandsessionholder", "brandservicesessionholder",
                "freedbrandsessionholder")):
            return "公众号"
        if "@chatroom" in username:
            return "群聊"
        if self._verify_flag.get(username, 0) & 0x8:
            return "公众号"
        return "个人"

    def display_name(self, username: str) -> str:
        """联系人备注/昵称/微信号；查不到则原样返回 username。"""
        if self._contact:
            row = self._contact.execute(
                "SELECT remark, nick_name, alias FROM contact WHERE username=?",
                (username,)).fetchone()
            if row:
                for v in row:
                    if v:
                        return v
        return username

    def conversations(self, limit: int = 30, with_counts: bool = True) -> list[dict]:
        """最近活跃会话列表：username、显示名、分类、时间戳、最后摘要、消息总数。

        with_counts 与 LiveProvider 同名同义，只为接口一致：快照库是本地 sqlite，
        计数几乎免费，所以这里始终返回真实条数（不接受 None——消费方按整数格式化）。
        """
        out = []
        for username, ts, summary in (self._session or []).execute(
                "SELECT username, sort_timestamp, summary FROM SessionTable "
                "ORDER BY sort_timestamp DESC LIMIT ?", (limit,)):
            n = 0
            for table, db in self._tables_for(username):
                try:
                    n += db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    pass
            out.append(dict(username=username, display=self.display_name(username),
                            category=self.category(username), sort_ts=ts,
                            summary=(summary or "")[:40], msg_count=n))
        return out

    def messages(self, username: str, days: int | None = None,
                 limit: int = 500, include_non_text: bool = False) -> list[dict]:
        """时间正序的消息：{ts, who('我'/'对方'), name, text}。

        include_non_text=True 时，图片/语音/视频/表情/链接/系统消息以
        「[图片]」等占位符计入（用于会话浏览；LLM 分析默认仍只取文本）。
        群消息的发送者昵称取自消息自带前缀；单聊用 conversation 显示名。
        days 过滤按 create_time；zstd 行自动解压，解压失败跳过。
        """
        self_id = None
        cutoff = int(time.time() - days * 86400) if days else 0
        type_filter = "" if include_non_text else f"local_type={MSG_TYPES_TEXT} AND "
        merged, seen = [], set()
        non_text = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]",
                    49: "[链接/卡片]", 48: "[位置]", 10000: "[系统]", 51: "[状态]"}
        for table, db in self._tables_for(username):
            if self_id is None:
                self_id = self._self_id_in(db)
            try:
                rows = db.execute(
                    f"SELECT create_time, message_content, WCDB_CT_message_content, "
                    f"real_sender_id, local_type FROM {table} WHERE {type_filter}"
                    f"(create_time IS NULL OR create_time > ?) "
                    f"ORDER BY sort_seq DESC LIMIT ?",
                    (cutoff, limit)).fetchall()
            except sqlite3.Error:
                continue
            for ts, content, ct, sender, ltype in rows:
                if ct:
                    content = _decompress(content)
                if not content:
                    continue
                mine = self_id is not None and sender == self_id
                display = ""
                if ltype == MSG_TYPES_TEXT:
                    display, body = _strip_sender_prefix(content)
                    body = body.strip()
                    if not body:
                        continue
                    text = body
                elif include_non_text and ltype in non_text:
                    text = non_text[ltype]
                else:
                    continue
                key = (ts, sender, hash(text))
                if key in seen:   # 分片重叠去重
                    continue
                seen.add(key)
                merged.append((ts or 0, mine, display or "", text))
        merged.sort(key=lambda r: r[0])
        merged = merged[-limit:]
        other = self.display_name(username)
        return [dict(ts=ts, who="我" if mine else "对方",
                     name=("我" if mine else (display or other)), text=text)
                for ts, mine, display, text in merged]

    def matches_recent(self, username: str, text: str, limit: int = 60) -> bool:
        """触发文本是否出现在该会话最近 limit 条文本消息里（缓存路径的快速核验）。"""
        if not text or len(text.strip()) < 4:
            return False
        needle = "".join(text.split())
        for content, _sender in self._recent_rows(username, limit):
            if needle in "".join(_strip_sender_prefix(content)[1].split()):
                return True
        return False

    def context_for(self, username: str, n: int = 10,
                    max_chars: int = MAX_CONTEXT_CHARS) -> str | None:
        """最近 n 条文本消息 → 「我/对方(昵称): 内容」对话串。"""
        rows = self._recent_rows(username, n * 3)
        if not rows:
            return None
        lines: list[str] = []
        total = 0
        for content, mine in reversed(rows[-n:]):   # 时间正序
            display, body = _strip_sender_prefix(content)
            body = body.strip()
            if not body:
                continue
            if mine:
                who = "我"
            elif display:
                who = f"对方({display})"
            else:
                who = "对方"
            lines.append(f"{who}: {body}")
            total += len(lines[-1]) + 1
            if total >= max_chars:
                break
        text = "\n".join(lines[-n:])
        return text or None


# ---------- CLI 冒烟 ----------

def _selftest() -> int:
    import datetime
    p = HistoryProvider()
    print(f"消息库连接数: {len(p._msg_dbs)}, Name2Id 条数: {len(p._name2id)}")
    ok = True

    # 1) md5 表名映射验证：抽 3 个会话 username 检查表是否存在
    checked = 0
    for username, in (p._session or []).execute(
            "SELECT username FROM SessionTable ORDER BY sort_timestamp DESC LIMIT 50"):
        if username.startswith("gh_"):
            continue
        if p._tables_for(username):
            checked += 1
        if checked >= 3:
            break
    print(f"md5 表名映射抽检: {checked}/3 命中", "✓" if checked == 3 else "✗")
    ok &= checked == 3

    # 2) 撞名统计（回答"标题相同怎么办"的现实规模）
    if p._contact:
        dup = p._contact.execute(
            "SELECT nick_name, COUNT(*) c FROM contact WHERE nick_name!='' "
            "GROUP BY nick_name HAVING c>1 ORDER BY c DESC LIMIT 3").fetchall()
        total_dup = p._contact.execute(
            "SELECT COUNT(*) FROM (SELECT nick_name FROM contact WHERE nick_name!='' "
            "GROUP BY nick_name HAVING COUNT(*)>1)").fetchone()[0]
        print(f"撞名昵称组数: {total_dup}（示例: {dup}）")

    # 3) L1 内容反查：取一条真实消息当「OCR 触发文本」+ 干扰候选，看能否唯一命中
    probe_db = p._msg_dbs[0]
    table = probe_db.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'Msg_%' "
        "AND name NOT LIKE '%\\_%' ESCAPE '\\' ORDER BY name LIMIT 1").fetchone()
    if table:
        tname = table[0]
        row = probe_db.execute(
            f"SELECT message_content FROM {tname} WHERE local_type=1 "
            f"AND WCDB_CT_message_content=0 AND length(message_content)>12 "
            f"ORDER BY sort_seq DESC LIMIT 1").fetchone()
        if row:
            _, body = _strip_sender_prefix(_decompress(row[0]))
            # 从同一 username 反查（模拟：标题候选有多个，文本锁定）
            username = next((u for u, t in ((u, f"Msg_{hashlib.md5(u.encode()).hexdigest()}")
                              for u in p._name2id) if t == tname), None)
            if username:
                others = [u for u in p._recent_sessions(15) if u != username][:4]
                got = p._filter_by_text([username, *others], body)
                print(f"L1 内容反查: 命中={'✓' if got == username else '✗'}"
                      f"（候选 {1 + len(others)} 个）")
                ok &= got == username

    # 4) 上下文格式化
    demo = p._recent_sessions(8)
    for username in demo:
        if p._tables_for(username):
            ctx = p.context_for(username, n=6)
            n_lines = len(ctx.splitlines()) if ctx else 0
            print(f"context_for({username[:20]}…): "
                  f"{'✓ %d 行 / %d 字' % (n_lines, len(ctx)) if ctx else '✗ 空'}")
            break
    p.close()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="本地会话历史上下文（原型）")
    ap.add_argument("--db-dir", default=str(DEFAULT_DB_DIR))
    ap.add_argument("--self", default=None, dest="self_wxid",
                        help="自己的 wxid（不填则从解密目录名自动推断）")
    ap.add_argument("--title", default="", help="微信窗口标题（备注/昵称/群名）")
    ap.add_argument("--text", default="", help="OCR 触发消息文本（同名消歧用）")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    p = HistoryProvider(args.db_dir, args.self_wxid)
    username = p.resolve(args.title, args.text)
    if not username:
        print("未能定位会话（返回 None，面板按无上下文继续）")
        return 1
    print(f"定位会话: {username}")
    ctx = p.context_for(username, n=args.n)
    print("--- context ---")
    print(ctx or "(空)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
