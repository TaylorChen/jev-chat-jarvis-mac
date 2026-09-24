"""Offline launcher regression. Run with python probe/bootstrap_regression.py.

Uses zsh if available, otherwise bash (or --shell PATH). curl, brew, uv,
osascript, HOME and the venv are isolated fixtures: nothing is downloaded or
installed. The .app shell bootstrap is extracted from the actual build heredoc;
the native Mach-O launcher is outside this offline test's scope.
"""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = None

PRELUDE = r'''
export HOME="$PWD/home"
export TMPDIR="$PWD/tmp"
export FIXTURE_ROOT="$PWD"
export TRACE="$PWD/trace"
mkdir -p "$HOME" "$TMPDIR"
case "$SCENARIO" in
    existing|existing-venv|existing-old)
        mkdir -p "$HOME/.local/bin"
        cp "$FIXTURE_ROOT/uv" "$HOME/.local/bin/uv"
        chmod +x "$HOME/.local/bin/uv" ;;
esac
# 已有 venv（解释器正确）：用来验证「依赖清单变了要不要重同步」这一条
if [ "$SCENARIO" = existing-venv ]; then
    mkdir -p "$HOME/Library/Application Support/jev-jarvis/venv/bin"
    cp "$FIXTURE_ROOT/python" "$HOME/Library/Application Support/jev-jarvis/venv/bin/python"
    chmod +x "$HOME/Library/Application Support/jev-jarvis/venv/bin/python"
fi
# bash lacks zsh's print builtin. Production script is unchanged.
if [ -n "${BASH_VERSION:-}" ]; then
    print() { [ "${1:-}" != -r ] || shift; [ "${1:-}" != -- ] || shift; printf '%s\n' "$*"; }
fi
command() {
    if [ "${1:-}" = -v ] && [ "${2:-}" = uv ]; then
        [ -f "$HOME/.local/bin/uv" ] || return 1
        printf '%s\n' "$HOME/.local/bin/uv"
    elif [ "${1:-}" = -v ] && [ "${2:-}" = brew ]; then
        case "$SCENARIO" in brew|brew-fails|install-fails-brew) ;; *) return 1 ;; esac
        printf '%s\n' brew
    else
        builtin command "$@"
    fi
}
curl() {
    printf 'curl %s\n' "$*" >> "$TRACE"
    local output=""
    while [ "$#" -gt 0 ]; do
        case "$1" in -o|--output) output="$2"; shift ;; esac
        shift
    done
    if [ -n "$output" ]; then cat "$FIXTURE_ROOT/installer" > "$output"; fi
    case "$SCENARIO" in
        timeout|brew|brew-fails) return 28 ;;
        download-*) return "${SCENARIO#download-}" ;;
    esac
    if [ -z "$output" ]; then cat "$FIXTURE_ROOT/installer"; fi
}
brew() {
    printf 'brew %s\n' "$*" >> "$TRACE"
    [ "$SCENARIO" != brew-fails ] || return 9
    mkdir -p "$HOME/.local/bin"
    cp "$FIXTURE_ROOT/uv" "$HOME/.local/bin/uv"
    chmod +x "$HOME/.local/bin/uv"
}
uv() { "$HOME/.local/bin/uv" "$@"; }
osascript() { printf 'osascript %s\n' "$*" >> "$TRACE"; }
shasum() {
    case "$3" in
        */uv.lock) printf '%s  %s\n' "$EXPECTED_LOCK_SHA" "$3"; return ;;
    esac
    if [ "$SCENARIO" = checksum-bad ]; then
        printf '%064d  %s\n' 0 "$3"
    else
        printf '%s  %s\n' "$EXPECTED_UV_SHA" "$3"
    fi
}
source "$1"
'''

INSTALLER = r'''#!/bin/sh
echo installer-ran >> "$TRACE"
echo "install-dir=$UV_INSTALL_DIR modify-path=$UV_NO_MODIFY_PATH" >> "$TRACE"
case "$SCENARIO" in
    install-fails|install-fails-brew) echo 'binary download failed' >&2; exit 7 ;;
    missing-binary) exit 0 ;;
esac
mkdir -p "$HOME/.local/bin"
cp "$FIXTURE_ROOT/uv" "$HOME/.local/bin/uv"
chmod +x "$HOME/.local/bin/uv"
'''

UV = r'''#!/bin/sh
echo "uv $*" >> "$TRACE"
case "$1" in
    --version)
        if [ "$SCENARIO" = existing-old ]; then echo 'uv 0.1.0';
        else echo "uv ${EXPECTED_UV_VERSION}"; fi ;;
    sync)
        mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
        cp "$FIXTURE_ROOT/python" "$UV_PROJECT_ENVIRONMENT/bin/python"
        chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python" ;;
    run) echo app-started >> "$TRACE" ;;
esac
'''

# 记下真正启动了什么、拿到的是哪份配置：venv 版本探测（-c）必须回一个与包内钉住
# 一致的版本号，否则永远走「重建」分支，锁文件那套逻辑就测不到。
PYTHON = r'''#!/bin/sh
if [ "$1" = "-c" ]; then echo 3.12; exit 0; fi
echo "app-started source=${JEV_SOURCE:-unset}" >> "$TRACE"
'''

# 随包 uv.lock 的内容无所谓，只要稳定：探针按它的哈希算戳。
UV_LOCK = "version = 1\n"


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def run_fixture(scenario, source=False, prepare=None, env_extra=None):
    """跑一次启动脚本，返回 (completed, trace, log, stamp)。

    stamp 是启动脚本写下的依赖戳（tempdir 随函数结束删除，所以在返回前读出来）。
    prepare(root) 在脚本执行前准备 fixture（写配置、写戳）；env_extra 追加环境变量。
    """
    with tempfile.TemporaryDirectory(prefix="jev-bootstrap-") as td:
        root = Path(td)
        build = (ROOT / "packaging/build_app.sh").read_text(encoding="utf-8")
        launcher = build.split("<<'LAUNCHER'\n", 1)[1].split("\nLAUNCHER", 1)[0]
        launcher = launcher.replace("@PYTHON_PIN@", "3.12")
        # zsh accepts this one-line function without a semicolon; bash needs it.
        launcher = launcher.replace('>> "$LOG" }', '>> "$LOG"; }')
        app = root / "jev test.app/Contents"
        (app / "Resources/app").mkdir(parents=True)
        write(app / "Resources/launcher.zsh", launcher)
        # 随包依赖清单：启动脚本按它的哈希决定要不要重新同步依赖
        write(app / "Resources/app/uv.lock", UV_LOCK)
        write(root / "source/start.command", (ROOT / "start.command").read_text(encoding="utf-8"))
        helper = ROOT / "packaging/bootstrap_uv.sh"
        if helper.exists():
            for dest in (app / "Resources/app/packaging/bootstrap_uv.sh",
                         root / "source/packaging/bootstrap_uv.sh"):
                write(dest, helper.read_text(encoding="utf-8"))
        write(root / "installer", INSTALLER)
        write(root / "uv", UV)
        write(root / "python", PYTHON)
        if prepare:
            prepare(root)
        target = "source/start.command" if source else "jev test.app/Contents/Resources/launcher.zsh"
        helper_text = (ROOT / 'packaging/bootstrap_uv.sh').read_text(encoding='utf-8')
        version = re.search(r'^JEV_UV_VERSION="([^"]+)"', helper_text, re.M)
        digest = re.search(r'^JEV_UV_INSTALLER_SHA256="([0-9a-f]{64})"', helper_text, re.M)
        env = dict(os.environ, SCENARIO=scenario,
                   EXPECTED_UV_VERSION=version.group(1) if version else 'missing',
                   EXPECTED_UV_SHA=digest.group(1) if digest else '0' * 64,
                   EXPECTED_LOCK_SHA=lock_hash())
        # 外层环境里的 XDG 配置目录不许漏进 fixture：生产脚本会优先读它，
        # 否则「默认读 ~/.config」这条断言会随开发者机器而变。
        env.pop("XDG_CONFIG_HOME", None)
        env.update(env_extra or {})
        # Use a relative path so Git Bash and native POSIX shells share the same fixture.
        completed = subprocess.run([SHELL, "-c", PRELUDE, target, target], cwd=root, env=env,
                                   capture_output=True, text=True, encoding="utf-8", timeout=15)
        trace = (root / "trace").read_text(encoding="utf-8") if (root / "trace").exists() else ""
        log = root / "home/Library/Logs/jev-jarvis.log"
        detail = log.read_text(encoding="utf-8") if log.exists() else ""
        stamp_path = root / "home/Library/Application Support/jev-jarvis/uv.lock.sha256"
        stamp = stamp_path.read_text(encoding="utf-8").strip() if stamp_path.exists() else None
        return completed, trace, detail, stamp


def run_launcher(scenario, source=False):
    completed, trace, detail, _stamp = run_fixture(scenario, source=source)
    return completed, trace, detail


def lock_hash():
    import hashlib
    return hashlib.sha256(UV_LOCK.encode()).hexdigest()


class BootstrapRegression(unittest.TestCase):
    def test_checksum_mismatch_never_executes_installer(self):
        completed, trace, log = run_launcher("checksum-bad")
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("installer-ran", trace)
        self.assertNotIn("app-started", trace)
        self.assertIn("SHA256", log + completed.stdout + trace)

    def test_existing_wrong_uv_version_is_not_accepted(self):
        completed, trace, log = run_launcher("existing-old")
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("app-started", trace)
        self.assertIn("版本", log + completed.stdout + trace)

    def test_app_executes_successfully_downloaded_installer(self):
        completed, trace, log = run_launcher("success")
        self.assertIn("installer-ran", trace, "Downloaded installer was never executed")
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertIn("app-started", trace)
        self.assertIn("modify-path=1", trace)

    def test_source_launcher_uses_the_same_successful_bootstrap(self):
        completed, trace, log = run_launcher("success", source=True)
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertIn("installer-ran", trace)
        self.assertIn("app-started", trace)

    def test_timeout_never_executes_a_partial_download(self):
        for source in (False, True):
            with self.subTest(source=source):
                completed, trace, log = run_launcher("timeout", source=source)
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("installer-ran", trace)
                self.assertNotIn("app-started", trace)
                self.assertIn("超时", log + completed.stdout + trace)
                self.assertIn("--connect-timeout", trace)
                self.assertIn("--max-time", trace)
                self.assertIn("--retry", trace)

    def test_installer_failure_is_reported(self):
        completed, trace, log = run_launcher("install-fails")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("installer-ran", trace)
        self.assertIn("binary download failed", log)
        self.assertNotIn("app-started", trace)

    def test_download_failure_never_runs_unpinned_homebrew(self):
        for source in (False, True):
            with self.subTest(source=source):
                completed, trace, log = run_launcher("brew", source=source)
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("installer-ran", trace)
                self.assertNotIn("brew install uv", trace)
                self.assertNotIn("app-started", trace)

    def test_existing_uv_skips_installation(self):
        for source in (False, True):
            with self.subTest(source=source):
                completed, trace, log = run_launcher("existing", source=source)
                self.assertEqual(completed.returncode, 0, completed.stderr + log)
                self.assertNotIn("curl ", trace)
                self.assertNotIn("installer-ran", trace)
                self.assertNotIn("brew install", trace)
                self.assertIn("app-started", trace)

    def test_specific_download_errors_are_reported(self):
        for code, expected in ((6, "DNS"), (7, "无法连接"), (22, "HTTP"),
                               (23, "磁盘"), (60, "证书")):
            with self.subTest(code=code):
                completed, trace, log = run_launcher(f"download-{code}")
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("installer-ran", trace)
                self.assertIn(expected, log)
                self.assertNotIn("app-started", trace)

    def test_success_exit_without_uv_is_not_accepted(self):
        completed, trace, log = run_launcher("missing-binary")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("uv 版本为 不可用", log)
        self.assertNotIn("app-started", trace)

    def test_download_failure_preserves_the_original_reason(self):
        completed, trace, log = run_launcher("brew-fails")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("超时", log)
        self.assertNotIn("brew install", trace)
        self.assertNotIn("app-started", trace)

    def test_installer_execution_failure_does_not_fall_back_to_unpinned_brew(self):
        completed, trace, log = run_launcher("install-fails-brew")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("installer-ran", trace)
        self.assertNotIn("brew install uv", trace)
        self.assertNotIn("app-started", trace)


class DependencySyncRegression(unittest.TestCase):
    """已存在的 venv 不会因为随包 uv.lock 变了就补依赖，这里钉住那套戳逻辑。

    真实事故：zstandard 加进 pyproject 后，老 venv 静默缺包，数据库直读只会少消息
    （压缩行解不开就跳过），界面上看不出任何异常。
    """

    def test_changed_lock_resyncs_without_rebuilding_the_venv(self):
        def prepare(root):
            stamp = root / "home/Library/Application Support/jev-jarvis/uv.lock.sha256"
            write(stamp, "打错的旧戳\n")
        completed, trace, log, stamp = run_fixture("existing-venv", prepare=prepare)
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertIn("uv sync", trace)
        self.assertNotIn("rm -rf", log)          # 原地同步，不重建（不必重下 torch）
        self.assertIn("app-started", trace)
        self.assertEqual(stamp, lock_hash())

    def test_unchanged_lock_skips_the_sync(self):
        def prepare(root):
            stamp = root / "home/Library/Application Support/jev-jarvis/uv.lock.sha256"
            write(stamp, lock_hash() + "\n")
        completed, trace, log, _stamp = run_fixture("existing-venv", prepare=prepare)
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertNotIn("uv sync", trace)
        self.assertIn("app-started", trace)

    def test_missing_stamp_syncs_once_then_records_it(self):
        # 升级上来的老用户没有戳：同步一次（依赖齐全时 uv 是快操作），并把戳补上
        completed, trace, log, stamp = run_fixture("existing-venv")
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertIn("uv sync", trace)
        self.assertEqual(stamp, lock_hash())


class ConfigPathRegression(unittest.TestCase):
    """配置目录必须和设置窗口写的那份一致（XDG 优先），否则界面里改了不生效。"""

    def test_default_config_is_dot_config(self):
        def prepare(root):
            write(root / "home/.config/jev-jarvis/env", "export JEV_SOURCE=db\n")
        completed, trace, log, _stamp = run_fixture("existing", prepare=prepare)
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertIn("source=db", trace)

    def test_xdg_config_wins_over_dot_config(self):
        def prepare(root):
            write(root / "home/.config/jev-jarvis/env", "export JEV_SOURCE=ocr\n")
            write(root / "xdg/jev-jarvis/env", "export JEV_SOURCE=db\n")
        completed, trace, log, _stamp = run_fixture(
            "existing", prepare=prepare, env_extra={"XDG_CONFIG_HOME": "xdg"})
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertIn("source=db", trace)

    def test_no_config_file_still_starts(self):
        completed, trace, log, _stamp = run_fixture("existing")
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertIn("source=unset", trace)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shell", default=shutil.which("zsh") or shutil.which("bash"))
    args, rest = parser.parse_known_args()
    SHELL = args.shell
    if not SHELL:
        parser.error("zsh or bash is required (or supply --shell PATH)")
    unittest.main(argv=[__file__] + rest)
