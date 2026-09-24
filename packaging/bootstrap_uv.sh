#!/bin/sh
# Shared by start.command and the packaged launcher. Source this file, then call
# jev_ensure_uv LOG_PATH. Failures leave a user-facing reason in JEV_UV_ERROR.

JEV_UV_VERSION="0.12.18"
JEV_UV_INSTALLER_URL="https://github.com/astral-sh/uv/releases/download/0.12.18/uv-installer.sh"
JEV_UV_INSTALLER_SHA256="e62a5ea089de9c6a9b1f767166c9f33194e18d350e9f375c6355fac3ba0fb42b"

jev_uv_version() {
    command -v uv >/dev/null 2>&1 || return 1
    uv --version 2>/dev/null | awk '{print $2}'
}

jev_ensure_uv() {
    local uv_log="$1" install_script="" curl_code=0 install_code=0 found="" actual_sha=""
    JEV_UV_ERROR=""
    found="$(jev_uv_version || true)"
    if [ "$found" = "$JEV_UV_VERSION" ]; then
        return 0
    fi
    if [ -n "$found" ]; then
        printf '%s\n' "uv 版本 $found 不符合固定版本 $JEV_UV_VERSION，将安装固定版本" >> "$uv_log"
    fi

    printf '%s\n' "正在下载 Astral 官方固定版本 uv $JEV_UV_VERSION 安装脚本" >> "$uv_log"
    if install_script=$(mktemp "${TMPDIR:-/tmp}/jev-uv.XXXXXX"); then
        # Download completely before execution: curl | sh can report success when
        # curl fails, or execute a truncated script. Keep curl's original exit code.
        if curl -LsSf --connect-timeout 10 --max-time 60 \
                --retry 2 --retry-delay 1 --retry-max-time 120 \
                -o "$install_script" "$JEV_UV_INSTALLER_URL" >> "$uv_log" 2>&1; then
            actual_sha="$(LC_ALL=C LANG=C shasum -a 256 "$install_script" 2>/dev/null | awk '{print $1}')"
            if [ "$actual_sha" != "$JEV_UV_INSTALLER_SHA256" ]; then
                JEV_UV_ERROR="uv 安装脚本 SHA256 校验失败，已拒绝执行"
                printf '%s\n' "$JEV_UV_ERROR" >> "$uv_log"
                rm -f "$install_script"
                return 1
            fi
            # Pin the destination to the PATH used by both Finder and source runs;
            # do not depend on a shell-profile edit taking effect in this process.
            if UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 \
                    sh "$install_script" >> "$uv_log" 2>&1; then
                found="$(jev_uv_version || true)"
                if [ "$found" = "$JEV_UV_VERSION" ]; then
                    uv --version >> "$uv_log" 2>&1
                    rm -f "$install_script"
                    return 0
                fi
                JEV_UV_ERROR="官方安装脚本已结束，但 uv 版本为 ${found:-不可用}，需要 $JEV_UV_VERSION"
            else
                install_code=$?
                JEV_UV_ERROR="uv 官方安装脚本执行失败（退出码 $install_code），请查看日志中的二进制下载或权限错误"
            fi
        else
            curl_code=$?
            case "$curl_code" in
                28) JEV_UV_ERROR="uv 安装脚本下载超时，请检查网络或代理" ;;
                5|6) JEV_UV_ERROR="uv 下载地址或代理无法解析，请检查 DNS 和代理设置" ;;
                7) JEV_UV_ERROR="无法连接 uv 下载服务器，请检查网络或代理" ;;
                35|60) JEV_UV_ERROR="uv 下载的 TLS/证书校验失败，请检查系统时间、证书或代理" ;;
                22) JEV_UV_ERROR="uv 下载服务器返回 HTTP 错误，请查看日志" ;;
                23) JEV_UV_ERROR="无法保存 uv 安装脚本，请检查磁盘空间和临时目录权限" ;;
                *) JEV_UV_ERROR="uv 安装脚本下载失败（curl 退出码 $curl_code），请查看日志" ;;
            esac
        fi
        rm -f "$install_script"
    else
        JEV_UV_ERROR="无法创建 uv 安装临时文件，请检查临时目录权限和磁盘空间"
    fi

    printf '%s\n' "$JEV_UV_ERROR" >> "$uv_log"
    return 1
}
