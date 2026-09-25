"""Settings editor and explicit network probes; never mutates running credentials."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import urllib.error
import urllib.parse

import userconfig
from generate import (_endpoint, http_post_json, validate_transport_url,
                      Generator, ThinkingOnlyError)

PREFIXES = ("TYPESAFE", "OPENAI", "ANTHROPIC")
FIELDS = ("API_KEY", "BASE_URL", "MODEL")
# 非模型设置：感知数据源与密钥文件位置。它们和数据源标签页一起保存，走同一套
# 「只改被编辑的行、其余原样保留」的写回逻辑。
EXTRA_KEYS = ("JEV_SOURCE", "JEV_KEYS_FILE")
SOURCES = ("ocr", "db")
DEFAULTS = {
    "TYPESAFE": ("https://api.typesafe.ai", "jev-latest"),
    "OPENAI": ("https://api.openai.com/v1", ""),
    "ANTHROPIC": ("https://api.anthropic.com", ""),
}
ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z_0-9]*)(\s*=\s*)(.*)$")


def read_document(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def validate_source(value: str) -> str:
    """数据源只能是 ocr / db；写进配置前先挡下来，免得启动时静默回退。"""
    value = (value or "").strip().lower()
    if value not in SOURCES:
        raise ValueError("数据源只能填 ocr 或 db。")
    return value


def write_settings(path: Path, original: str, changes: dict[str, str]) -> str:
    """Change only edited assignments, preserve other lines, replace atomically at 0600."""
    if read_document(path) != original:
        raise ValueError("配置文件已被其他程序修改，请关闭设置窗口后重新打开。")
    allowed = {f"{p}_{f}" for p in PREFIXES for f in FIELDS} | set(EXTRA_KEYS)
    if not changes.keys() <= allowed:
        raise ValueError("不支持的配置项。")
    for value in changes.values():
        if any(c in value for c in "\r\n\0"):
            raise ValueError("配置值不能含换行或空字符。")
    if "JEV_SOURCE" in changes:
        changes = dict(changes, JEV_SOURCE=validate_source(changes["JEV_SOURCE"]))
    remaining = dict(changes)
    lines = []
    for line in original.splitlines(keepends=True):
        match = ASSIGNMENT.match(line.rstrip("\r\n"))
        if match and match[2] in changes:
            key = match[2]
            # Keep even duplicate assignments consistent, so shell and Python agree.
            _, comment = userconfig.split_env_comment(match[4])
            ending = "\n" if line.endswith("\n") else ""
            line = f"{match[1]}{key}{match[3]}{shlex.quote(changes[key])}"
            line += (" " + comment if comment else "") + ending
            remaining.pop(key, None)
        lines.append(line)
    text = "".join(lines)
    if remaining:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "".join(f"export {k}={shlex.quote(v)}\n" for k, v in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return text


def _allow_jev_private_http(prefix: str) -> bool:
    return (prefix == "TYPESAFE"
            and userconfig.get("JEV_ALLOW_INSECURE_HTTP").strip().lower()
            in ("1", "true", "yes", "on"))


def validate_endpoint(base: str, *, allow_private_http: bool = False) -> str:
    return validate_transport_url(base, allow_private_http=allow_private_http)


def list_models(prefix: str, base: str, key: str) -> list[str]:
    """GET the provider's models endpoint. No presets, redirects or alternate service."""
    allow_private_http = _allow_jev_private_http(prefix)
    base = validate_endpoint(base, allow_private_http=allow_private_http)
    if not key:
        raise ValueError("请先填写密钥；Ollama 可填写 ollama。")
    api = "anthropic" if prefix == "ANTHROPIC" else "openai"
    url = _endpoint(base, api).rsplit("/", 1)[0]
    if api == "openai":
        url = url.removesuffix("/chat")
    url += "/models"
    headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
               if api == "anthropic" else {"authorization": f"Bearer {key}"})
    offered = []
    after = None
    while True:
        p = urllib.parse.urlsplit(url)
        path = p.path + ("?after_id=" + urllib.parse.quote(after, safe="") if after else "")
        cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
        conn = cls(p.hostname, p.port, timeout=15)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if resp.status >= 300:
                raise urllib.error.HTTPError(url, resp.status, "", resp.headers, None)
            data = json.loads(resp.read())
        finally:
            conn.close()
        # TypeSafe documents {models: [{name, description, release_date}]};
        # OpenAI/Anthropic use {data: [{id, ...}]}. Do not guess alternate schemas.
        collection, field = ("models", "name") if prefix == "TYPESAFE" else ("data", "id")
        offered.extend(m[field] for m in data.get(collection, [])
                       if isinstance(m, dict) and isinstance(m.get(field), str) and m[field])
        if api != "anthropic" or not data.get("has_more"):
            break
        next_id = data.get("last_id")
        if not next_id or next_id == after:
            raise ValueError("模型列表分页返回异常，请手动填写模型。")
        after = next_id
    if not offered:
        raise ValueError("服务未返回模型列表，请手动填写模型。")
    return sorted(set(offered))


def test_connection(prefix: str, base: str, key: str, model: str, extra: dict | None = None) -> None:
    """Use exactly the unsaved form values; never fall back to built-in credentials."""
    allow_private_http = _allow_jev_private_http(prefix)
    base = validate_endpoint(base, allow_private_http=allow_private_http)
    if not key or not model.strip():
        raise ValueError("请填写密钥和模型后再测试。")
    if prefix == "TYPESAFE":
        # Same endpoint/transport as JevJudge, without loading the local judge model.
        body = {"model": model, "state": "你好", "questions": {
            "test": {"type": "choice", "instructions": "请选择问候", "criteria": {"问候": None}}}}
        data = http_post_json(base + "/v1/systemone", {
            "content-type": "application/json", "authorization": f"Bearer {key}"},
            body, 30, allow_private_http=allow_private_http)
        if ((data.get("answers") or {}).get("test") or {}).get("choice") != "问候":
            raise ValueError("服务返回了响应，但未返回有效判断结果。")
        return
    api = "anthropic" if prefix == "ANTHROPIC" else "openai"
    body = {"model": model, "max_tokens": 300, "temperature": 0.9,
            "messages": [{"role": "user", "content": "请只回复：连接成功"}]}
    headers = {"content-type": "application/json"}
    if api == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        body.update(extra or {})
        # Testing must exercise the model the user selected, not an extra-body override.
        body.update(model=model, stream=False)
        headers["authorization"] = f"Bearer {key}"
    data = http_post_json(_endpoint(base, api), headers, body, 30)
    if api == "anthropic":
        raw = "".join(p.get("text", "") for p in data.get("content", []) if isinstance(p, dict))
    else:
        raw = Generator._openai_json(data, model, "非思考模型")
    if not raw.strip():
        raise ValueError("服务未返回文字；请检查模型是否支持生成，或关闭思考模式。")


def error_message(error: Exception) -> str:
    """Never display raw remote bodies, URLs or exception strings containing credentials."""
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}：请检查地址、密钥及模型权限。"
    if isinstance(error, ThinkingOnlyError):
        return "模型只返回了思考内容，没有正文；请关闭思考模式或更换模型。"
    if isinstance(error, (TimeoutError, OSError, http.client.HTTPException)):
        return "连接失败或超时，请检查服务地址和网络。"
    return "请求未得到有效结果，请检查地址、模型及服务是否支持该接口。"
