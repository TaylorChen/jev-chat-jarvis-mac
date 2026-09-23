#!/usr/bin/env python3
"""live_db.py — 微信「运行中」加密库的实时读取 Provider（与 HistoryProvider 同接口）。

原理：用 wcdb-key-tool 提取的每库密钥 + sqlcipher 只读打开微信正在使用的
SQLCipher 加密库，直接读取实时消息（WAL 最新数据照读，绝不写微信文件）。

库选择规则（微信 4.x 按会话类别分库）：
  个人/群聊  → message/message_*.db
  公众号     → message/biz_message_*.db
每个会话按时间分片存在于其中若干个库，读取时跨库合并、按时间去重排序。

依赖：brew install sqlcipher；zstandard（解压 WCDB 压缩消息）。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import sys
import json
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

sys_path = str(Path(__file__).parent)
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

import wechat_keys  # noqa: E402


def _resolve_sqlcipher() -> str:
    """优先 PATH 上的 sqlcipher（Apple Silicon brew 在 /opt/homebrew，Intel 在 /usr/local）。"""
    for c in (shutil.which("sqlcipher"), "/opt/homebrew/bin/sqlcipher",
              "/usr/local/bin/sqlcipher"):
        if c and Path(c).exists():
            return c
    return "sqlcipher"          # 交给 PATH 解析；找不到时子进程报错，由调用方降级


SQLCIPHER = _resolve_sqlcipher()
XWECHAT_FILES = Path.home() / ("Library/Containers/com.tencent.xinWeChat/Data/"
                               "Documents/xwechat_files")
# 密钥文件位置由 wechat_keys 统一决定（显式配置 > 应用数据目录 > 旧手工布局）
DEFAULT_KEYS = wechat_keys.keys_file()
CIPHER_PRAGMAS = (
    "PRAGMA cipher_page_size = 4096;\n"
    "PRAGMA kdf_iter = 256000;\n"
    "PRAGMA cipher_hmac_algorithm = HMAC_SHA512;\n"
    "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;\n"
)
NON_TEXT = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]",
            49: "[链接/卡片]", 48: "[位置]", 10000: "[系统]", 51: "[状态]"}
MSG_TEXT = 1

try:
    import zstandard as _zstd
    _dctx_tls = threading.local()

    def _get_dctx():
        """ZstdDecompressor 非线程安全：每个线程独立实例。"""
        dctx = getattr(_dctx_tls, "dctx", None)
        if dctx is None:
            dctx = _zstd.ZstdDecompressor()
            _dctx_tls.dctx = dctx
        return dctx
except ImportError:
    def _get_dctx():
        return None


def _decompress(content):
    """WCDB_CT=4 的行是 zstd 帧；失败返回空串。"""
    if isinstance(content, bytes):
        dctx = _get_dctx()
        if dctx is None:
            return ""
        try:
            return dctx.decompress(content).decode("utf-8", "replace")
        except Exception:
            return ""
    return content or ""


def _looks_like_raw_id(name: str) -> bool:
    """原始 wxid / openim id / 纯数字 id：这些不该出现在面板和判断 prompt 里。

    群消息的说话人写在正文前缀里（`昵称:\n正文`），但有些成员在群里的昵称就是自己的
    原始 id（实测 `25984981997518334@openim`），面板上「来自 2598498…@openim」既看不懂
    也进不了判断的价值。contact.db 里通常有真名，用它换掉。
    """
    n = (name or "").strip()
    if not n:
        return False
    return (n.endswith("@openim") or n.endswith("@chatroom")
            or n.startswith("wxid_") or n.isdigit())


def _row_content(ct: int, raw) -> str:
    """一行 message_content → 文本。

    WCDB_CT_message_content 非空表示 zstd 压缩，查询里已 hex() 成字符串；
    压缩行解不开就返回空串（跳过该行），不要把它当明文塞进上下文。
    """
    if not ct:
        return raw or ""
    if not raw:
        return ""
    try:
        return _decompress(bytes.fromhex(raw))
    except (TypeError, ValueError):
        return ""


def _strip_sender_prefix(text: str) -> tuple[str, str]:
    head, sep, rest = text.partition(":\n")
    if sep and head and "\n" not in head and len(head) <= 64:
        return head, rest
    return "", text


def key_script(enc_key: str, sql: str) -> str:
    """sqlcipher 输入脚本：raw key + 微信的 cipher pragmas + 一条查询。

    引号是这里唯一的坑，必须精确：PRAGMA key = "x'<hex>'"; —— raw key 用
    双引号包裹、内部是 x'...' 十六进制字面量，**分号在双引号之外**。
    写成 "x'<hex>';" 会让 sqlcipher 把分号当进字面量，报 near "PRAGMA":
    syntax error 后接 file is not a database，看起来像密钥不对，实际是引号错位。
    """
    return ('PRAGMA key = "x\'' + enc_key + '\'";\n'
            + CIPHER_PRAGMAS
            + ".mode json\n" + sql.rstrip(";\n") + ";\n")


def find_live_dir() -> Path | None:
    import glob as _glob
    pattern = str(XWECHAT_FILES / "*" / "db_storage")
    hits = [Path(p) for p in _glob.glob(pattern)]
    for h in hits:
        if (h / "session" / "session.db").exists():
            return h
    return hits[0] if hits else None


class LiveProvider:
    """实时消息供给者（conversations/messages/display_name/category）。"""

    def __init__(self, live_dir=None, keys_file=None, self_wxid=None):
        self.live_dir = Path(live_dir) if live_dir else find_live_dir()
        if not self.live_dir:
            raise RuntimeError("未找到 live db_storage，请确认微信 4.x 已登录过")
        try:
            import wechat_keys as _wk
            keys_file = _wk.keys_file()
        except ImportError:
            keys_file = (Path.home() / "Library/Application Support/"
                         "jev-jarvis/wechat_keys.json")
        raw = json.loads(keys_file.read_text()) if keys_file.exists() else {}
        self.keys = {k: v for k, v in raw.items() if isinstance(v, dict)}
        self.self_wxid = self_wxid or self._detect_self_wxid()
        self._contact = {}
        self._db_tables = {}     # rel -> (集合, 时间戳)；空集合 + 时间戳 = 60s 内不重扫
        self._self_ids = {}      # db_filename -> self rowid
        self._load_contact()
        # 启动自检：认不出「自己」时每一条消息都会被当成对方（连自己的话一起分析），
        # 是静默错方向的故障——在这里暴露成 self_id_ok，由调用方决定怎么提示。
        self.self_id_ok = self._probe_self_id()

    # ---------- 基础 ----------

    def _detect_self_wxid(self) -> str:
        import re
        m = re.match(r"(.+)_\d{1,4}$", self.live_dir.parent.name)
        return m.group(1) if m else None

    def _probe_self_id(self, probes: int = 3) -> bool:
        """前几个消息库里能否解析出自己的 sender_id（每库独立，解析出一个即可）。

        优先个人/群聊库（message_*.db）：公众号库（biz_message_*.db）里通常只有
        公众号自己发言，拿它当自检样本会误报。
        """
        rels = [r for r in sorted(self.keys,
                                  key=lambda r: (r.startswith("message/biz_"), r))
                if r.startswith("message/")][:probes]
        return any(self._self_id_of_rel(rel) is not None for rel in rels)

    def _load_contact(self):
        for r in (self._query("contact/contact.db",
                              "SELECT username, remark, nick_name, verify_flag "
                              "FROM contact") or []):
            self._contact[r["username"]] = (r.get("remark") or "",
                                            r.get("nick_name") or "",
                                            int(r.get("verify_flag") or 0))

    @staticmethod
    def _sqlstr(s: str) -> str:
        return "'" + str(s).replace("'", "''") + "'"

    def display_name(self, username: str) -> str:
        remark, nick, _v = self._contact.get(username, ("", "", 0))
        return remark or nick or username

    def category(self, username: str) -> str:
        if (username.startswith("gh_") or username in (
                "brandsessionholder", "brandservicesessionholder",
                "freedbrandsessionholder")):
            return "公众号"
        if "@chatroom" in username:
            return "群聊"
        if int(self._contact.get(username, ("", "", 0))[2]) & 0x8:
            return "公众号"
        return "个人"

    # ---------- sqlcipher 查询（-readonly 直开 live 库，绝不写微信文件） ----------

    def _query(self, rel_path: str, sql: str, attempts: int = 4) -> list[dict]:
        """sqlcipher -readonly 直接打开 live 加密库（实时、零拷贝、绝不写微信文件）。

        微信活跃写入时只读打开可能间歇性失败（合并/检查点窗口），
        自动重试即可穿过；仍失败则返回 []，下一轮轮询会再试。
        """
        key_entry = self.keys.get(rel_path)
        if not key_entry:
            return []
        live_file = self.live_dir / rel_path
        if not live_file.exists():
            return []
        script = key_script(key_entry["enc_key"], sql)
        for _attempt in range(attempts):
            try:
                r = subprocess.run(
                    [SQLCIPHER, "-readonly", str(live_file)], input=script,
                    capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                time.sleep(0.6)
                continue
            out = r.stdout
            if "[" in out and "]" in out:
                try:
                    return json.loads(out[out.find("["): out.rfind("]") + 1])
                except json.JSONDecodeError:
                    pass
            time.sleep(0.4)
        return []

    def _tables_of(self, rel_path: str) -> set:
        """该库中所有 Msg_* 表名，缓存为 (表集合, 时间戳)。

        空结果也缓存 60s：轮询每跳都要为会话找分片，一次读失败若每跳重扫
        20–30 个库的 sqlite_master，就是每跳几十个 sqlcipher 子进程。
        60s 后空结果作废重试（可能是微信当时正在合并、也可能是真的没有消息表）。
        """
        cached = self._db_tables.get(rel_path)
        if cached is not None and (cached[0] or time.time() - cached[1] < 60):
            return cached[0]
        rows = self._query(rel_path,
                           "SELECT name FROM sqlite_master "
                           "WHERE type='table' AND name LIKE 'Msg_%'")
        tables = {r["name"] for r in rows} if rows else set()
        self._db_tables[rel_path] = (tables, time.time())
        return tables

    def _tables_stale(self, rel_path: str) -> bool:
        """该库的表集合是否需要（重新）扫一遍。"""
        hit = self._db_tables.get(rel_path)
        return hit is None or (not hit[0] and time.time() - hit[1] >= 60)

    def _shards_for(self, username: str) -> list[tuple[str, str]]:
        """username 的会话表所在的所有库分片: [(rel_path, table)]。

        冷启动要为几十个库各开一次 sqlcipher 才能知道谁有这个会话表，串行实测
        ~8s——而这是轮询会走到的路径，所以未命中的库并发扫。全命中（常态）
        时一个线程池都不建。
        """
        table = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        rels = [f"message/{f.name}"
                for f in sorted((self.live_dir / "message").glob("*.db"))
                if f"message/{f.name}" in self.keys]
        stale = [rel for rel in rels if self._tables_stale(rel)]
        if len(stale) > 1:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(6, len(stale))) as ex:
                list(ex.map(self._tables_of, stale))
        elif stale:
            self._tables_of(stale[0])
        return [(rel, table) for rel in rels if table in self._tables_of(rel)]

    def _self_id_of_rel(self, rel: str) -> int | None:
        """该库 Name2Id 里自己的 rowid（sender_id，每库独立）。

        列名是 user_name，不是 username——contact.db 的 contact 表用 username，
        消息库的 Name2Id 用 user_name，写错只会静默返回空（sqlcipher 报
        no such column，_query 吞掉），后果是**每条消息都被判成对方**。
        所以这里两种列名都试，命中即缓存；两个都失败不缓存（下一轮再试）。
        """
        if rel in self._self_ids:
            return self._self_ids[rel]
        for col in ("user_name", "username"):
            rows = self._query(rel,
                               "SELECT rowid AS rid FROM Name2Id "
                               f"WHERE {col}={self._sqlstr(self.self_wxid)}")
            if rows:
                rid = int(rows[0]["rid"])
                self._self_ids[rel] = rid
                return rid
        return None

    # ---------- 会话列表 ----------

    def conversations(self, limit: int = 60, with_counts: bool = False) -> list[dict]:
        """最近活跃会话列表。

        with_counts 默认关：live 每个库一次 sqlcipher 子进程，逐会话数总条数
        在会话列表上要跑上百次，UI 加载时等不起（history_ui 只在快照模式要计数，
        那里是本地 sqlite，几乎免费）。CLI 冒烟和 probe/live_db_smoke.py 显式传 True，
        此时逐会话并发数，几十个会话约一两秒。
        """
        rows = self._query(
            "session/session.db",
            "SELECT username, sort_timestamp, summary FROM SessionTable "
            f"ORDER BY sort_timestamp DESC LIMIT {int(limit)}")
        counts = self._count_many([r["username"] for r in rows]) if with_counts else {}
        return [dict(username=r["username"], display=self.display_name(r["username"]),
                     category=self.category(r["username"]),
                     sort_ts=int(r["sort_timestamp"] or 0),
                     summary=(r.get("summary") or "")[:40],
                     msg_count=counts.get(r["username"]))
                for r in rows]

    def _count(self, username: str) -> int:
        """该会话在所有时间分片里的消息总数（每分片一次查询）。"""
        n = 0
        for rel, table in self._shards_for(username):
            rows = self._query(rel, f"SELECT COUNT(*) AS c FROM {table}")
            n += rows[0]["c"] if rows else 0
        return n

    def _count_many(self, usernames: list[str], workers: int = 6) -> dict:
        """并发数多个会话：每个会话是若干 sqlcipher 子进程，串行数十个会话要十几秒。

        失败（返回 0）不区分「真的空」和「读失败」，计数只用于展示与增长对比，
        不值得为它加重试语义；_query 自己已经重试过。
        """
        if not usernames:
            return {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(usernames))) as ex:
            return dict(zip(usernames, ex.map(self._count, usernames)))

    # ---------- 消息 ----------

    def messages(self, username: str, days: int | None = None,
                 limit: int = 500, include_non_text: bool = True) -> list[dict]:
        """跨时间分片合并的最近消息（时间正序）。who=我/对方。"""
        cutoff = int(time.time() - days * 86400) if days else 0
        shards = self._shards_for(username)
        merged, seen = [], set()
        for rel, table in shards:
            rows = self._query(
                rel,
                f"SELECT create_time, WCDB_CT_message_content AS ct, real_sender_id, "
                f"local_type, CASE WHEN WCDB_CT_message_content THEN "
                f"hex(message_content) ELSE message_content END AS content "
                f"FROM {table} WHERE local_type IN (1,3,34,43,47,49) "
                f"AND (create_time IS NULL OR create_time > {int(cutoff)}) "
                f"ORDER BY sort_seq DESC LIMIT {int(limit)}")
            for r in rows:
                content = _row_content(int(r["ct"] or 0), r["content"])
                if not content:
                    continue
                ltype = int(r["local_type"])
                sender = int(r["real_sender_id"]) if r["real_sender_id"] is not None else None
                mine = sender is not None and sender == self._self_id_of_rel(rel)
                display, body = _strip_sender_prefix(content)
                body = body.strip()
                if not body:
                    continue
                if ltype != MSG_TEXT and ltype in NON_TEXT:
                    body = NON_TEXT[ltype]
                key = (r["create_time"], sender, hash(body))
                if key in seen:
                    continue
                seen.add(key)
                who = "我" if mine else "对方"
                if mine:
                    name = "我"
                elif display and not _looks_like_raw_id(display):
                    name = display                      # 群里的群昵称，信息量最大
                else:
                    # 没有前缀（单聊）或前缀就是原始 id：查 contact.db 要真名
                    name = self.display_name(display or username)
                merged.append(dict(ts=int(r["create_time"] or 0), who=who,
                                   name=name, text=body))
        merged.sort(key=lambda m: m["ts"])
        return merged[-limit:]


def main() -> int:
    p = LiveProvider()
    convs = p.conversations(8, with_counts=True)
    print(f"live 库: {p.live_dir}\n最近会话:")
    for i, c in enumerate(convs, 1):
        ts = datetime.fromtimestamp(c["sort_ts"]).strftime("%m-%d %H:%M") if c["sort_ts"] else "?"
        print(f"  {i}. {c['display']} ({c['category']}, {c['msg_count']}条, {ts})")
    newest, newest_c = 0, ""
    for c in convs:
        for m in p.messages(c["username"], limit=1):
            if m["ts"] > newest:
                newest, newest_c = m["ts"], c["display"]
    if newest:
        age = (time.time() - newest) / 60
        print(f"\n全场最新消息: {datetime.fromtimestamp(newest):%m-%d %H:%M:%S}"
              f"（{age:.1f} 分钟前）→ {'✓ 实时' if age < 600 else '⚠ 偏旧'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
