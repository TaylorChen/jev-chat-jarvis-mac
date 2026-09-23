# 第三方组件：wcdb-key-tool（macOS 版）

本目录是**原样引入（vendored）**的第三方工具，用来提取**你自己账号**的微信
SQLCipher 数据库密钥；本项目只调用它，不修改它。

- 上游：https://github.com/TANGandXUE/wcdb-key-tool
- 上游提交：`79f1b5b92e12c66aa281b4a60a3c478b5f547dfa`
- 许可：MIT（见同目录 `LICENSE`，版权归 CloudDreamAI / TANGandXUE）
- 引入的文件：`wcdb_key_tool_macos.py`（仅 macOS 版；本项目只支持 macOS）
- SHA256（`wcdb_key_tool_macos.py`）：
  `419347b39c5d61751417c149c015b6ae05ce6020e193fbbff2f3a0fa7b6e567e`

## 为什么不改这个文件

保持与上游逐字节一致，才能用上面的 SHA256 核对「有没有被本地改动」，也才能在
上游更新时直接重新复制覆盖。本项目自己的逻辑（路径、调用、状态展示）都在
`src/wechat_keys.py` 和 `tools/extract_wechat_keys.command` 里，不往这里加代码。

## 更新方式

```bash
git clone https://github.com/TANGandXUE/wcdb-key-tool /tmp/wcdb-key-tool
cp /tmp/wcdb-key-tool/wcdb_key_tool_macos.py tools/wcdb_key_tool/
cp /tmp/wcdb-key-tool/LICENSE tools/wcdb_key_tool/
shasum -a 256 tools/wcdb_key_tool/wcdb_key_tool_macos.py   # 更新本文件里的哈希
git -C /tmp/wcdb-key-tool rev-parse HEAD                   # 更新本文件里的提交号
```

## 它做了什么（以及需要什么权限）

- 微信 4.0.x：在微信进程内存里扫描已经缓存成明文的 raw key。
- 微信 4.1.10+：内存里只剩 passphrase，工具用 LLDB 在
  `CCKeyDerivationPBKDF` 上断一次点，读出 passphrase，再按每个库自己的 salt
  做 PBKDF2-HMAC-SHA512（256,000 轮）派生出该库的密钥，最后用数据库首页的
  HMAC 校验「这把钥匙真的对」。
- 因此它需要 `sudo`（`task_for_pid` / lldb attach），并且要先把微信 ad-hoc
  重签名去掉 Hardened Runtime：
  `sudo codesign --force --deep --sign - /Applications/WeChat.app`
  ——这会改动 `/Applications/WeChat.app` 的签名，微信自动更新后可能需要重做。
- 首次提取需要你在微信里**退出登录再重新登录**一次（触发密钥重新计算，断点才
  有机会命中）。抓到的 passphrase 缓存在 `~/.wcdb-key-tool/wechat-passphrase.json`
  （0600，仅当前用户可读），之后不必重复。

本项目的默认感知路径（OCR）**不使用**这个工具、不需要上面任何权限；只有把数据源
切到「数据库直读」才会用到。详见 README「数据源」一节。
