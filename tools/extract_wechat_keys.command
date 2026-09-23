#!/bin/zsh
# 提取微信数据库密钥（数据库直读模式的前提）——OCR 数据源不需要运行这个脚本。
#
# 它会做两件事，两件都需要 sudo，所以放在终端里跑而不是藏在界面按钮后面：
#   1. 给 /Applications/WeChat.app 做 ad-hoc 重签名，去掉 Hardened Runtime
#      （微信自动更新后可能需要重做）
#   2. 用本项目内置的 wcdb-key-tool（tools/wcdb_key_tool/，原样引入的第三方工具）
#      提取每个库的密钥；首次需要你在微信里退出登录再重新登录一次
#
# 提取出的密钥与解密快照都写到应用数据目录（仓库之外），并把属主改回当前用户，
# 否则 sudo 生成的文件只有 root 能读，应用反而读不到。
set -e
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"

APP_SUPPORT="$HOME/Library/Application Support/jev-jarvis"
KEYS_FILE="$APP_SUPPORT/wechat_keys.json"
DECRYPTED_DIR="$APP_SUPPORT/decrypted"
TOOL="$ROOT/tools/wcdb_key_tool/wcdb_key_tool_macos.py"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    print -r -- "未找到 python3：请先安装 Xcode Command Line Tools（xcode-select --install）"
    exit 1
fi
if [ ! -f "$TOOL" ]; then
    print -r -- "内置工具缺失：$TOOL"
    exit 1
fi
if [ ! -d /Applications/WeChat.app ]; then
    print -r -- "未找到 /Applications/WeChat.app（本工具只支持正式安装的 macOS 微信）"
    exit 1
fi
# 只有「首次抓 passphrase」（微信 4.1.10+）那一步需要 lldb；已缓存密钥或 passphrase、
# 或微信仍是 4.0.x（内存里有明文 raw key）时都不需要。所以这里只提示不拦：
# 真走到那一步时上游工具自己会报「未检测到 lldb，请安装 Xcode Command Line Tools」。
if [ -z "$(xcode-select -p 2>/dev/null)" ] || ! xcrun -f lldb >/dev/null 2>&1; then
    if [ ! -s "$HOME/.wcdb-key-tool/wechat-passphrase.json" ]; then
        print -r -- "提示：没有 lldb（Xcode Command Line Tools，xcode-select --install）。"
        print -r -- "      已缓存密钥/passphrase、或微信是 4.0.x 时不需要它；只有「首次抓 passphrase」才需要。"
        print -rn -- "      仍要继续？[y/N] "
        read -r want_lldb
        case "$want_lldb" in
            [yY]*) ;;
            *) print -r -- "已取消。先运行 xcode-select --install 再试。"; exit 1 ;;
        esac
    fi
fi
if ! command -v sqlcipher >/dev/null 2>&1; then
    print -r -- "提示：本项目读取加密库还需要 sqlcipher（提取密钥本身不需要它）。"
    print -rn -- "现在用 Homebrew 安装？[y/N] "
    read -r want_brew
    case "$want_brew" in
        [yY]*)
            if command -v brew >/dev/null 2>&1; then
                brew install sqlcipher || print -r -- "安装失败；可稍后手动执行 brew install sqlcipher"
            else
                print -r -- "未找到 Homebrew；装好 Homebrew 后执行 brew install sqlcipher，或先只用 OCR 读屏。"
            fi ;;
        *) print -r -- "跳过安装：本次仍可提取密钥，但「数据库直读」要等 sqlcipher 装好才可用。" ;;
    esac
fi

print -r -- "即将："
print -r -- "  1) sudo codesign --force --deep --sign - /Applications/WeChat.app"
print -r -- "     （微信会被 ad-hoc 重签名；微信自动更新后需要重做这一步）"
print -r -- "  2) sudo $PY $TOOL extract --output $KEYS_FILE --decrypt"
print -r -- "     （首次会等你在微信里退出登录再登录一次，最多等 180 秒）"
print -r -- ""
print -r -- "只提取你自己账号、你自己设备上的密钥；不会发送任何数据。"
print -rn -- "继续？[y/N] "
read -r answer
case "$answer" in
    [yY]*) ;;
    *) print -r -- "已取消。"; exit 1 ;;
esac

mkdir -p "$APP_SUPPORT"

print -r -- "\n[1/3] 重签名微信（去 Hardened Runtime）…"
sudo codesign --force --deep --sign - /Applications/WeChat.app

print -r -- "\n[2/3] 提取密钥（接下来请按提示在微信里退出登录再重新登录）…"
sudo "$PY" "$TOOL" extract --output "$KEYS_FILE" --decrypt

print -r -- "\n[3/3] 把生成的结果交还给当前用户…"
sudo chown -R "$(id -u):$(id -g)" "$KEYS_FILE" "$APP_SUPPORT/decrypted" 2>/dev/null || true
[ -f "$KEYS_FILE" ] && chmod 600 "$KEYS_FILE"
[ -d "$DECRYPTED_DIR" ] && chmod 700 "$DECRYPTED_DIR"

print -r -- "\n完成。密钥：$KEYS_FILE"
print -r -- "解密快照：$DECRYPTED_DIR"
print -r -- "现在可以在「模型设置 → 数据源」里选「数据库直读」，或设置 JEV_SOURCE=db 后重启。"
