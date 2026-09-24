#!/usr/bin/env python3
"""wechat_keys.py — 微信数据库密钥的存放位置、状态与提取入口。

默认数据源就是「数据库直读」，因此本模块默认参与启动：有密钥就直接读微信本地库，
没有就退回 OCR 读屏并把原因讲清楚（既不静默降级，也不让应用起不来）。数据库直读
要每个库各自的一把密钥（由 `tools/wcdb_key_tool/` 里原样引入的 wcdb-key-tool
提取），本模块负责三件事：

  * 决定密钥文件与显式解密快照放在哪 —— 个人数据一律在仓库之外
  * 判断「现在有没有可用密钥」，好让界面和日志给出能照做的提示，而不是等读取失败
  * 给出提取命令（含 sudo 与重签名前提），由 `tools/extract_wechat_keys.command` 执行

配置（都在 `~/.config/jev-jarvis/env` 或 `~/Library/Application Support/jev-jarvis/env`）：
  JEV_KEYS_FILE   密钥文件路径（默认见 KEYS_FILE）
  JEV_DB_DIR      解密快照目录（历史分析用；默认见 DECRYPTED_DIR）
  JEV_LIVE_DIR    微信 db_storage 路径（默认自动定位）

CLI（自检）：
  uv run python src/wechat_keys.py             # 状态 + 下一步该做什么
  uv run python src/wechat_keys.py --command   # 只打印提取命令
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys_path = str(Path(__file__).parent)
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

import userconfig  # noqa: E402

# macOS 应用数据目录（与 venv 同一处，见 README「卸载」一节）
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "jev-jarvis"
KEYS_FILE = APP_SUPPORT / "wechat_keys.json"
DECRYPTED_DIR = APP_SUPPORT / "decrypted"
# 早期手工布局（wcdb-key-tool 直接 clone 到 ~/python 时留下的路径）：仍然读，
# 但新提取一律写应用数据目录，避免用户以为「必须把工具 clone 到某个位置」。
LEGACY_DIR = Path.home() / "python" / "wcdb-key-tool"
LEGACY_KEYS_FILE = LEGACY_DIR / "all_keys.json"
LEGACY_DECRYPTED_DIR = LEGACY_DIR / "decrypted"

# 上游工具缓存的 passphrase（只判断存在，绝不读取内容）
PASSPHRASE_FILE = Path.home() / ".wcdb-key-tool" / "wechat-passphrase.json"
PROJECT = Path(__file__).resolve().parent.parent
TOOL = PROJECT / "tools" / "wcdb_key_tool" / "wcdb_key_tool_macos.py"
EXTRACT_COMMAND = PROJECT / "tools" / "extract_wechat_keys.command"


def _configured(name: str) -> Path | None:
    value = userconfig.get(name).strip()
    if not value:
        return None
    return Path(value).expanduser()


def keys_file() -> Path:
    """密钥文件：显式配置 > 应用数据目录（已存在）> 旧手工布局（已存在）> 应用数据目录。"""
    explicit = _configured("JEV_KEYS_FILE")
    if explicit:
        return explicit
    if KEYS_FILE.exists():
        return KEYS_FILE
    if LEGACY_KEYS_FILE.exists():
        return LEGACY_KEYS_FILE
    return KEYS_FILE


def decrypted_dir() -> Path:
    """解密快照目录：显式配置 > 应用数据目录（已存在）> 旧手工布局（已存在）> 应用数据目录。"""
    explicit = _configured("JEV_DB_DIR")
    if explicit:
        return explicit
    if DECRYPTED_DIR.exists():
        return DECRYPTED_DIR
    if LEGACY_DECRYPTED_DIR.exists():
        return LEGACY_DECRYPTED_DIR
    return DECRYPTED_DIR


def live_dir() -> Path | None:
    """微信 live db_storage：显式配置优先，否则交给 live_db 自动定位。"""
    return _configured("JEV_LIVE_DIR")


def key_count(path: Path | None = None) -> int:
    """密钥文件里可用的库数；文件缺失/损坏都算 0（不抛异常，状态展示用）。"""
    path = path or keys_file()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(raw, dict):
        return 0
    return sum(1 for v in raw.values()
               if isinstance(v, dict) and v.get("enc_key"))


def has_keys() -> bool:
    return key_count() > 0


def lldb_path() -> str | None:
    """lldb 可执行文件；**只在「首次抓 passphrase」时才是必需的**。

    上游工具是四级路径：①已缓存的密钥 → ②已缓存的 passphrase + PBKDF2 →
    ③扫内存里的明文 raw key（微信 4.0.x）→ ④LLDB 断点抓 passphrase（微信 4.1.10+）。
    只有第 ④ 级需要 lldb（由 Xcode Command Line Tools / Xcode 提供）。所以缺 lldb
    不是致命错误：该报错的地方上游脚本自己会报（"未检测到 lldb，请安装 Xcode
    Command Line Tools"），我们只负责提前说清楚。
    """
    try:
        proc = subprocess.run(["xcrun", "-f", "lldb"], capture_output=True,
                              text=True, timeout=10)
        found = (proc.stdout or "").strip()
        if proc.returncode == 0 and found and Path(found).exists():
            return found
    except (OSError, subprocess.SubprocessError):
        pass
    # /usr/bin/lldb 在没装开发者工具时只是个报错的 shim，所以不能只 which()
    try:
        proc = subprocess.run(["lldb", "--version"], capture_output=True,
                              text=True, timeout=15)
        if proc.returncode == 0:
            return shutil.which("lldb") or "lldb"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def passphrase_cached() -> bool:
    """上游工具是否缓存了 passphrase（缓存了就不必再走 lldb 那条路）。只看大小。"""
    try:
        return PASSPHRASE_FILE.stat().st_size > 0
    except OSError:
        return False


def sqlcipher_path() -> str | None:
    """读取加密库要用的 sqlcipher 可执行文件；没有就返回 None。

    注意：**提取密钥不需要它**（内置的第三方工具自己用 CommonCrypto 派生 + HMAC 校验），
    但本项目的读取器每次查询都要起一个 sqlcipher 子进程。新机器上装了密钥却没装
    `brew install sqlcipher` 时，db 模式会以 FileNotFoundError 静默失败——所以这里显式
    检查，并让 resolve_source 据此回退、让设置页把安装命令写出来。
    """
    from live_db import _resolve_sqlcipher      # 函数级导入：live_db 反过来依赖本模块
    path = _resolve_sqlcipher()
    return path if Path(path).exists() else None


def resolve_source(cli: str | None = None) -> tuple[str, str]:
    """定下这次启动用哪个数据源，返回 (source, 给日志的一句话)。

    两种情况都退回 OCR 并说明原因：取值写错、以及选了 db 但还没提取密钥。
    静默退回会让用户以为「切了没生效」，所以原因必须能打出来。
    """
    source = userconfig.perception_source(cli)
    if source not in userconfig.PERCEPTION_SOURCES:
        return "ocr", (f"数据源 {source!r} 不认识（只支持 ocr / db），已按 ocr 启动")
    if source == "db" and not has_keys():
        return "ocr", ("数据源 db 需要先提取密钥"
                       "（tools/extract_wechat_keys.command）；本次按 ocr 启动")
    if source == "db" and sqlcipher_path() is None:
        # 密钥有了但缺 sqlcipher：db 模式每一跳都会 FileNotFoundError，不如直接退回读屏
        # 并把安装命令说清楚（提取密钥那一步不需要它，所以用户很容易漏装）
        return "ocr", "数据源 db 需要 sqlcipher（brew install sqlcipher）；本次按 ocr 启动"
    return source, ""


def extraction_command(decrypt: bool = False) -> str:
    """给人照抄的命令（需要 sudo 与 lldb；首次还要退出登录再登录微信）。"""
    suffix = " --decrypt" if decrypt else ""
    return (f"sudo {sys.executable} {TOOL} extract "
            f"--output {keys_file()}{suffix}")


def status_line() -> str:
    """一眼能读懂的状态，UI 与日志共用。"""
    n = key_count()
    if n:
        line = f"已提取 {n} 个库的密钥（{keys_file()}）"
        return line if sqlcipher_path() else line + " · 但缺少 sqlcipher（brew install sqlcipher）"
    path = keys_file()
    if path.exists():
        return f"密钥文件不可用（{path}）——请重新提取"
    hint = ""
    if not passphrase_cached() and lldb_path() is None:
        # 只有「首次抓 passphrase」需要 lldb；没有它 + 没有缓存时先提醒，别等提取失败
        hint = "；首次提取需要 lldb（xcode-select --install）"
    return f"尚未提取密钥（数据库直读会退回 OCR 读屏）{hint}"


def doctor() -> int:
    """`python src/wechat_keys.py`：把「现在能不能用、下一步做什么」讲清楚。"""
    n = key_count()
    path = keys_file()
    print(f"密钥文件: {path}（{'存在' if path.exists() else '不存在'}）")
    print(f"状态: {status_line()}")
    print(f"显式解密快照目录: {decrypted_dir()}")
    print(f"内置工具: {TOOL}（{'存在' if TOOL.exists() else '缺失'}）")
    cipher = sqlcipher_path()
    print(f"sqlcipher: {cipher or '缺失 —— 需要 brew install sqlcipher（读取加密库用）'}")
    print(f"passphrase 缓存: {'已缓存（重新提取不必再走 lldb）' if passphrase_cached() else '无'}")
    lldb = lldb_path()
    print(f"lldb: {lldb or '缺失 —— 仅在「首次抓 passphrase」（微信 4.1.10+）时需要：xcode-select --install'}")
    if n:
        print("\n数据源默认就是 db，现在即可直读。想改回读屏：配置里写 JEV_SOURCE=ocr，"
              "或用 `uv run python src/hud.py --source ocr` 临时切换。")
        return 0
    print("\n还没有密钥：启动会按 ocr（读屏）跑，不影响使用。想用数据库直读就跑：")
    print(f"  {EXTRACT_COMMAND}")
    print("  （它会先 ad-hoc 重签名微信去掉 Hardened Runtime，再用 sudo 跑内置工具）")
    print("  然后按提示在微信里退出登录再重新登录一次（触发密钥重新计算）")
    print("\n只打印命令不改动任何东西：")
    print("  " + extraction_command())
    return 1


def main() -> int:
    if "--command" in sys.argv[1:]:
        print(extraction_command())
        return 0
    return doctor()


if __name__ == "__main__":
    sys.exit(main())
