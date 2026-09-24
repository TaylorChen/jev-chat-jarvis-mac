# jev-chat-jarvis（macOS）

微信弹出一条消息 → 悬浮窗立刻告诉你**这句话的真实意图**、**风险几级**、**该怎么回**。

**纯只读、零封号风险**——不注入、不 hook、不自动发送，也不改动微信的任何数据文件。默认**数据库直读**：用你自己的密钥只读打开微信本地库（消息更全、带真实上文），没提取过密钥就自动退回「看屏幕 + 本地模型判断」，两种方式都不需要注入类操作。见[数据源](#数据源数据库直读--ocr-读屏)。

![演示：微信群消息进来 → 面板给出意图、风险分级与候选回复 → 点「填入」直接进微信输入框](docs/demo.gif)

## 交流反馈

用着有问题、想提需求、想一起改，扫码进群（二维码 7 天失效，过期了在 issue 说一声）：

<img src="docs/wechat-group.png" width="200" alt="扫码加入微信交流群">

## 它能做什么

- **意图 + 风险**：8 类意图零样本 **86.4%**（22 条回归口径），风险 0–9 分级 + 行动建议，本地模型一次前向出全分布
- **候选回复**：内置 10 种话术并发生成（每条一稳一放各出 2 条）→ 先上屏 → 本地模型排序后原位重排；换话术立刻按当前消息重新生成
- **快**：消息一出现判断 + 生成同时起跑，M1 Pro 出意图 ~1.5 s、出候选 ~1.5–2 s（端到端为机制推算口径，以日志实测为准）
- **YOLO 检测框**（可选，`JEV_BOXES=1` 启动即开、菜单栏可切）：OCR 命中的消息实时框在微信窗口上，对方/我分色 + 置信度（只在读屏模式下有消息框；数据库直读没有屏幕坐标，只画输入目标）

## 数据源：数据库直读 / OCR 读屏

消息可以从两个地方来，**默认数据库直读**（没有密钥时自动退回读屏），两边都是只读：

| | 数据库直读（默认） | OCR 读屏（没有密钥时的退路，也可手动切回） |
|---|---|---|
| 怎么拿消息 | `sqlcipher -readonly` 打开微信本地库；消息表按时间分片，跨库合并去重 | 截取微信窗口 + Vision 识别 |
| 上下文 | 该会话真实历史（默认最近 100 条，判断与生成都用全部读到的那批，约 4k 字符 / ~2k tokens） | 只有屏幕上看得到的那几条（判断 2 条、生成 8 条） |
| 额外权限 | **不需要屏幕录制权限**（全程不截屏）；但要先提取密钥（见下）。「填入」优先走辅助功能 | 屏幕录制（+ 填入用辅助功能；没有辅助功能权限时会退回截屏视觉兜底） |
| 前置条件 | 运行一次 `tools/extract_wechat_keys.command` | 无 |
| 已知取舍 | 大库（几百 MB WAL 的公众号库）单次读取约几秒；窗口不在屏幕上时填不入 | 屏外消息看不到、微信改版会让布局常量失效 |

### 工作原理：数据库直读是怎么读到消息的

**库布局**（`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>_<hash>/db_storage/`）：

| 库 | 内容 |
|---|---|
| `message/message_0/1/2.db` | 个人与群聊消息，**按时间分片**（旧→新分布在 0→2），同一会话跨库 |
| `message/biz_message_0/1/2.db` | 公众号消息（同样分片） |
| `session/session.db` | 会话列表（SessionTable：最后活跃时间、摘要、清未读时间戳） |
| `contact/contact.db` | 联系人（备注/昵称，用于显示名解析） |
| `message/message_resource.db` | 消息资源索引（图片/视频的尺寸与状态） |
| `msg/attach/<hash>/<YYYY-MM>/Img/*.dat` | 图片缩略图与原图（**未加密 JPEG**，旧式 `_t_M.dat`；新式 `_t.dat` 为 V2 加密，暂不可解） |

**加密与打开**：每库是 SQLCipher 4（页 4096、HMAC-SHA512、PBKDF2-SHA512）。每个库有**各自的 32 字节 raw key**（互不相同），由一次抓取的 passphrase 对每库 salt 做 `PBKDF2-HMAC-SHA512(passphrase, salt, 256000)` 派生。读取 = `sqlcipher -readonly` 直接打开 live 库 + `PRAGMA key = "x'<raw_key>'"`——**零拷贝、绝不写微信文件**，已合并进主库的数据实时可读；仍在 `.material` 增量中的极新数据要等微信合并完成（通常几秒～几分钟），期间读取自动重试。

**消息表命名**：`Msg_<md5(username)>`，即会话 username 的 md5。一条图片消息的典型行：

```sql
CREATE TABLE Msg_48d3e17789e8816e9b1b6079fe641632(
    local_id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER,
    local_type INTEGER,          -- 1=文本 3=图片 34=语音 43=视频 47=表情 49=链接卡片 10000=系统
    sort_seq INTEGER,            -- 会话内排序键
    real_sender_id INTEGER,      -- → 本库 Name2Id 表的 rowid（发送者，每库独立）
    create_time INTEGER,         -- Unix 秒
    message_content TEXT,        -- 文本/（群消息自带「昵称:\n」前缀）/ XML
    WCDB_CT_message_content ..., -- 压缩标记：=4 时 message_content 是 zstd 帧（hex 传输，Python 侧解压）
    ...);
```

**四个关键细节**（都是实测踩出来的）：

1. **按会话分表 + 按时间分库**：同一会话的消息散布在 message_0/1/2 多个库里（旧分片→新分片），必须跨库合并按 create_time 排序去重，只读一个库会拿到「时间碎片」
2. **发送者解析每库独立**：消息行的 `real_sender_id` 是该库 `Name2Id` 表的 rowid（user_name → id 的映射**每个库各自独立**，不能跨库复用）；群消息正文自带 `昵称:` 前缀
3. **内容压缩**：`WCDB_CT_message_content = 4` 的行是 zstd 压缩帧（读出时以 hex 传输，Python 侧解压）；`= 0` 为明文
4. **合并窗口**：微信周期性把 `.material` 增量合并回主库，期间拷贝/打开会间歇失败（file is not a database）——读取代码自动重试穿过

**图片在哪**：`msg/attach/<md5(username)>/<YYYY-MM>/Img/` 下 `_t_M.dat`（缩略图）与 `_M.dat`（原图）是**未加密 JPEG**，按文件 mtime 就近匹配消息时间即可显示；新版 `_t.dat` 是 V2 加密格式（暂无法解出，对应消息显示 `[图片]` 占位）。

**触发语义**：默认 `follow` 模式只处理你最近打开过的会话（清未读时间最新者）的新到达对方文本消息；`JEV_DB_WATCH=all` 则所有会话的新消息都触发。

面板上随时能核对这次分析用了什么：**标题栏**写着数据源与条数（`数据库直读 N 条` / `OCR 读屏`），消息下面两行分别是**这段上下文的起止时间**（`历史 09-23 14:56 → 17:25 · 100 条`）和**JEV 的触发情况**（`JEV 已触发 17:20:31 · 停稳 1.2s 后上屏` / `JEV 判定完成 17:20:32 · 603ms` / `等待对方消息 · 到达即触发 JEV 预判`）。面板右上角**列表图标**或**「跟随哪个会话」有个诚实的前提**：微信只在**清未读**时记录时间戳。你点开一个**没有未读**的会话时，微信不写任何东西（实测：点开后该会话的时间戳原地不动），AX 也读不到会话列表（微信只暴露窗口按钮）——所以「自动跟随」只在你点开的会话有未读消息时成立。要一直盯住某个会话，点面板右上角的**图钉图标**（或菜单栏 **J →「跟随会话 ▸」**）从最近活跃会话里选一个（带未读数）——钉住后面板标题标 **· 手动**，选「自动」随时恢复；钉住期间只读你指定的那个会话。

菜单栏 **J →「查看读到的消息…」** 会把这一跳实际读到的消息原样列出来（会话名、时间、我/对方、正文，默认 100 条），打开时**默认停在最新一条**（不用手动往下滑），窗口开着时每读完一跳自动刷新——判断到底喂了什么上下文，看这个窗口就够了；日志里对应那行也会写「读到 N 条 · 上下文 M 条」。上下文条数可用 `JEV_CONTEXT_MESSAGES` 调（不填 = 数据库直读用全部读到）。

自动跟随的粒度由 `JEV_DB_WATCH` 控制：`follow`（默认）= 只分析你最近打开过的那个会话；`all` = 所有会话的新文本消息都触发分析（群多时会明显更吵）。写进 `~/.config/jev-jarvis/env` 重启生效。

切换方式（三种，优先级从高到低）：命令行 `uv run python src/hud.py --source ocr` → 配置项 `JEV_SOURCE=ocr` → 图形界面 **模型设置 → 感知 · 数据源**。写错的取值、或选了数据库直读但还没提取密钥，都会**明确退回读屏并在日志里说明原因**（没密钥时启动还会弹一次提示，告诉你去跑哪条命令），不会静默生效也不会每秒报错。

数据库直读需要每个库各自的密钥，由本项目内置的 [wcdb-key-tool](tools/wcdb_key_tool/NOTICE.md)（第三方 MIT 代码，原样引入、未改动）提取：

```bash
./tools/extract_wechat_keys.command     # 首次；会先重签名微信，再 sudo 提取密钥
```

- 它会 `sudo codesign --force --deep --sign - /Applications/WeChat.app` 去掉 Hardened Runtime，然后用 LLDB 在密钥派生处断一次点抓 passphrase，按每库 salt 做 PBKDF2 派生并用 HMAC 校验。**首次要按提示在微信里退出登录再重新登录一次**。微信自动更新后可能需要重做重签名。
- 密钥写到 `~/Library/Application Support/jev-jarvis/wechat_keys.json`（0600），passphrase 缓存在 `~/.wcdb-key-tool/wechat-passphrase.json`（0600），都在仓库之外；默认提取流程**不会生成明文数据库快照**。早期手工 clone 到 `~/python/wcdb-key-tool/` 的布局仍然兼容读取。
- 微信库始终以 `-readonly` 打开，我们不对微信文件做任何写入、不注入、不 hook，也不接触微信的网络通信。上述 `sudo` 与重签名只发生在提取密钥这一步。

### 分发给别人用之前，先看这一节

**数据库直读不是「开箱可用」**，它需要对方在自己机器上完成 4 个前置 + 1 个依赖，而且**会改动对方的微信安装**：

| 需要什么 | 说明 |
|---|---|
| macOS 微信 4.x（装在 `/Applications`，登录过） | 读取的是它自己的容器目录。**微信 3.x 与本项目不兼容**（数据目录、表结构、加密分片都不同） |
| Xcode Command Line Tools（**只有首次抓 passphrase 时才需要**） | 提供 `lldb`：`xcode-select --install`。上游工具是四级路径：已缓存密钥 → 已缓存 passphrase + PBKDF2 → 扫内存 raw key（微信 4.0.x）→ **LLDB 断点抓 passphrase（微信 4.1.10+，只有这一级需要 lldb）**。所以给别人装时：同一台机器第二次提取、或微信还是 4.0.x，都不需要它 |
| 管理员权限 + **给微信做一次 ad-hoc 重签名** | `sudo codesign --force --deep --sign - /Applications/WeChat.app`，去掉 Hardened Runtime——**这修改了对方的微信安装**，微信自动更新后要重做 |
| macOS 15+ 的「应用管理」权限（给执行命令的终端 App） | Sequoia 起修改 `/Applications` 内 App 的签名受 TCC 保护：`sudo codesign` 也会报 `Operation not permitted`。需在 系统设置 → 隐私与安全性 → 应用管理 给终端 App 授权（列表没有就 + 手动添加），**完全退出并重开终端**后生效；`xattr` 清理同样需要它 |
| 一次微信「退出登录 → 重新登录」 | 触发密钥重新计算，断点才能抓到（首次提取时） |
| `brew install sqlcipher` | **本项目读取**加密库用（上游工具自己解密，不需要它），所以最容易被漏装——漏了的话 db 模式会每跳失败；现在的做法是：启动时检测到缺它就自动退回读屏，并在日志/设置页写明 `brew install sqlcipher` |
| 重签名/提取后等 WCDB 增量合并结束 | 微信会把增量数据周期性合并回主库（`.material` 文件更新），合并窗口内拷贝主库会撕裂、读取报 file is not a database——代码已自动重试，若持续失败等几分钟再试 |

一键入口是这个脚本（会先检查 lldb / sqlcipher / WeChat.app，逐条确认后再动手）：

```bash
./tools/extract_wechat_keys.command          # 源码跑
# .app 里：模型设置 → 感知 · 数据源 → 「在终端里提取密钥…」
```

**不想折腾就完全不做这些**：不提取密钥 → 自动退回 OCR 读屏，只需要「屏幕录制」权限（「填入」另需「辅助功能」）。所以对外分发时的建议是：

1. 告诉对方**默认走读屏**（右键打开，未公证），够用且零风险；
2. 想用数据库直读再按上面表格走，并**明确告知会给他微信做一次 ad-hoc 重签名**；
3. 对方执行完可以用 `uv run python src/wechat_keys.py` 自查：会打印密钥文件、条数、内置工具、`sqlcipher` 是否就位。

同一套本地库还有两个命令行/窗口工具（不经过悬浮窗）：

```bash
uv run python src/history_analyze.py --live list --top 15      # 直接只读实时加密库
uv run python src/history_analyze.py --live summary "某人"      # LLM 议题/结论/承诺待办/风险
uv run python src/history_analyze.py --live intent  "某人"      # 对方消息抽样跑意图判断
uv run python src/history_analyze.py --live reply   "某人"      # 最新未回复消息 → 按话术生成候选
JEV_LIVE_DB=1 uv run python src/history_ui.py                   # 原生窗口：会话列表 + 三种分析
```

`summary`/`reply` 会把对话文本发给生成层配置的 LLM；`intent` 和悬浮窗判断在未配置 TypeSafe/Jev key 时使用本地模型，配置后会把判断上下文发给对应服务。默认密钥提取不创建快照；只有手工准备了仓库外快照时才省略 `--live`。

## 用法

**只想用**：[Releases](https://github.com/jev-chat/jev-chat-jarvis-mac/releases) 下载 `.app`，解压拖进「应用程序」，**第一次右键 → 打开**（没做公证，双击会被 Gatekeeper 拦）。

第一次启动会自动退回 OCR 读屏，并弹一次提示告诉你怎么开启数据库直读（在终端跑 `./tools/extract_wechat_keys.command`，然后用「模型设置 → 感知 · 数据源」切到数据库直读）；读屏路径需要「屏幕录制」权限（系统设置 › 隐私与安全性 › 录屏与系统录音，给 **jev-jarvis** 打开），**退出重开**生效。「填入」另需「辅助功能」权限，第一次点会弹系统授权框。v0.3.1 及更早的旧版本还需把 **python3.12** 那条一并打开。

缺少固定版本 `uv 0.12.18` 时，两种启动入口都会从 Astral 官方不可变 Release 下载对应安装脚本，校验内置 SHA256 后才执行并安装到 `~/.local/bin`；校验失败、版本不符或下载失败都会停止，不再回退执行浮动脚本或自动安装 Homebrew 最新版。详细输出见 `~/Library/Logs/jev-jarvis.log`。

`.app` 启动时会按包内 `uv.lock` 的哈希和已有运行环境核对一次：对得上就直接启动，对不上就**只补装依赖、不重建环境**（升级后首次启动可能多等几秒；只有解释器版本不对才会重建）。所以新版包新增的依赖不会因为「环境早就建好了」而静默缺失。

**从源码跑**（微信在运行；读屏模式另需终端已授予屏幕录制）：`./start.command`，参数会透传（如 `./start.command --source ocr`）。分层自测：

```bash
uv run python src/perception.py                  # 感知层：识别到的消息 + 耗时
uv run python src/judge.py "这个需求你今天跟一下"  # 单条消息出判断
uv run python src/judge_zh_test.py               # 22 条中文意图回归
uv run python src/generate.py --check            # 生成层凭据解析
uv run python src/wechat_keys.py                 # 数据源 db：密钥状态与下一步
uv run python -B -m unittest discover -s tests   # 发出消息/异步结果回归（合成 OCR，不读屏）
uv run python probe/bootstrap_regression.py      # 两种启动入口的离线回归；不联网、不实际安装
```

## 配置

两层、两个 key：判断层不填走本地 decider-2b（首次下载约 7 GB）；生成层不填则不生成候选，但感知和本地判断仍可使用。全部配置在一个 env 文件（**不提供第二种格式**）：

### 可视化配置（#18）

点击悬浮窗右上角 **齿轮图标（模型设置）**，或菜单栏 **J → 模型设置…**（菜单里还有「查看读到的消息…」），可编辑 Jev、OpenAI 兼容、Anthropic 兼容三组密钥、服务地址与模型，以及**感知 · 数据源**（OCR 读屏 / 数据库直读、密钥文件位置、在终端里提取密钥的入口）。
设置窗口显示在悬浮窗上方，不会被面板遮挡。**保存后必须退出并重新打开应用**；保存不会切换本次运行的配置。

- 窗口编辑 `$XDG_CONFIG_HOME/jev-jarvis/env`（未设置时为 `~/.config/jev-jarvis/env`），显示具体路径。只修改所编辑服务的字段，保留其他配置、注释和未识别行，文件权限设为 `600`。文件被其他程序修改时拒绝覆盖，需重新打开窗口。
- 填好地址与密钥，点击「获取模型列表」从该服务的 `/models` 接口动态获取，再下拉选择；不内置模型清单。Jev 按官方 `models[].name` 读取（当前列表为别名，未列出的版本号仍可手填）；OpenAI/Anthropic 按 `data[].id` 读取。接口不支持、失败或返回空列表时明确提示，仍可手填，不自动换模型或服务。空下拉显示「暂无」（仅作提示，不作为模型保存或调用），仍可手填；底部动态提示以蓝色显示进行状态、绿色显示成功、红色显示错误。列表可见不代表一定有生成权限，选定后再测试。
- 「测试连接」使用窗口内**尚未保存**的地址、密钥和模型发起实际调用，仅发送固定问候语，不读取微信内容；可能产生少量服务费用。生成层必须返回非空文字才算成功，不能用 `--check` 的配置解析成功代替连接成功。
- 密钥掩码显示；窗口仅读取所编辑文件中的值，不把环境变量或项目 `.env` 中的密钥复制进用户文件。各配置页顶部显示本次启动使用的实际来源；生成页同时标明当前启用的服务。
- 环境变量优先于用户 env，用户 env 优先于项目 `.env`；生成层 OpenAI 组优先于 Anthropic 组。两组均未配置时不发起生成请求。清空当前文件的密钥不会禁用其他来源中的密钥。由终端或启动器导出的值也显示为「环境变量」。
- API 格式由密钥组决定：`OPENAI_*` 使用 OpenAI 格式，`ANTHROPIC_*` 使用 Anthropic 格式；自定义地址不需要包含服务名称。Ollama 可填 `http://localhost:11434/v1`、密钥 `ollama`，模型从本地服务获取或手填。Jev 地址沿用判断层约定，不含末尾 `/v1`。
- 钥匙串：不新增钥匙串读写。如果原 env 用 `$(security find-generic-password …)` 等 shell 表达式提供密钥，窗口不执行表达式、不展示其内容，未输入新密钥时保留原行；仍由已有启动器执行。要在窗口测试该服务，需明确输入密钥；保存将用输入值替换原表达式。外部注入的密钥继续遵循环境变量优先级。
- `JEV_BOXES`、`JEV_TONES`、`OPENAI_EXTRA_BODY`、`JEV_DB_WATCH`、`JEV_LIVE_DIR`、`JEV_DB_DIR` 暂仍通过 env 配置，保存窗口不会改动它们（数据源与密钥文件位置已在「感知 · 数据源」页内可编辑）。OpenAI 连接测试沿用当前启动的 `OPENAI_EXTRA_BODY`；完整话术管理等留待后续扩展。

也可继续手动编辑：

```bash
mkdir -p ~/.config/jev-jarvis
cat > ~/.config/jev-jarvis/env <<'ENV'
# 判断层（可选）：TypeSafe Jev，不填用本地 decider-2b
export TYPESAFE_API_KEY=""

# 生成层：任意 OpenAI 兼容端点
export OPENAI_API_KEY="sk-你的key"
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_MODEL="deepseek-chat"
# 端点的思考模式要靠额外字段关时填（Qwen3 这类不关会慢几十倍）
# export OPENAI_EXTRA_BODY='{"enable_thinking":false}'
ENV
chmod 600 ~/.config/jev-jarvis/env
```

- **凭据解析以 key 为准**：提供 key 的来源同时决定端点和模型。实测可用：DeepSeek `deepseek-chat`（最快）；智谱 `glm-4-flash`（换 `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`/`ANTHROPIC_MODEL`，两组都填 OpenAI 组优先）；本地 Ollama `qwen2.5:7b`（完全不出网）
- **别用 thinking 模型**：思考吃光 `max_tokens`，候选 0 条，面板只报「候选生成失败」——DeepSeek 认准 `deepseek-chat`
- **自定义话术**：env 加一行 `JEV_TONES`（`|` 分隔、每条「名字=说明」，同名覆盖内置，重启生效），如 `摸鱼大师=像资深摸鱼选手，把活推得漂亮又不失礼`；说明写清「什么语气 + 别变成什么」最管用
- 自查凭据（不打印完整 key）：`uv run python src/generate.py --check`、`uv run python src/judge_jev.py`

## 磁盘占用与清理

| 内容 | 位置 | 大小 | 清理 |
|---|---|---|---|
| 判断层本地模型 `decider-2b`（不配判断层 key 才会下载，判断+排序共用） | `~/.cache/huggingface/hub/models--Mapika--decider-2b` | ~7 GB | `rm -rf ~/.cache/huggingface/hub/models--Mapika--decider-2b`；之后走本地判断会重新下载 |
| Python 运行环境（venv） | `~/Library/Application Support/jev-jarvis/venv` | ~0.7 GB | 删除 .app 不会连带删它，需手动删 |
| 数据库直读的密钥 | `~/Library/Application Support/jev-jarvis/wechat_keys.json` | ~5 KB | 删掉即退回 OCR 读屏；重新提取才会再有 |
| 显式创建的解密快照（默认不生成） | `JEV_DB_DIR` 指定的仓库外目录 | GB 级 | 确认不再需要后手工删除 |

生成层配 Ollama 的话模型在 Ollama 自己的目录（`~/.ollama`），非本项目下载。
密钥提取的 passphrase 缓存在 `~/.wcdb-key-tool/wechat-passphrase.json`（0600，内置的第三方工具所写）。

## 已知限制

- 默认的数据库直读：大库（公众号库 WAL 几百 MB）单次读取约几秒；冷启动第一次找会话分片要并发扫几十个库（实测 ~2 s）；窗口不在屏幕上时没有窗口几何，YOLO 框与「填入」不可用（消息分析不受影响）；微信自动更新后可能需要重新提取密钥。没提取密钥时自动退回读屏，此时下面的读屏限制才适用
- 读屏路径：收发方向靠文字位置判断，无法确认方向的文本标「方向未确认」，**不作为回复目标**（很宽的对方消息可能被跳过），只有明确识别为「对方」的消息才触发；图片/表情包读不出内容；引用回复当普通文本；公众号卡片可能被当消息解读；微信全屏布局下识别可能失效（布局常量待动态化，见 #17）；微信改版会让布局常量失效（`src/perception.py` 顶部常量需重新校准）；多窗口优先识别主窗口「微信 / WeChat」
- 启动后第一条判断慢是正常现象（本地模型预热）；不对劲先看日志（分阶段耗时、**不含消息正文**，可放心贴 issue）：`tail -40 ~/Library/Logs/jev-jarvis.log`

## 输入区检测框与填入

菜单栏「YOLO 检测框」同时显示消息框和输入目标，约每秒刷新：蓝色实线表示辅助功能接口定位到的输入控件，橙色虚线表示从截图边界推测的输入区；无法定位时显示原因。虚线不代表已取得可写控件，也不修复 #17 的聊天区域固定比例问题。

「填入」优先通过辅助功能接口写入并读回确认。部分微信版本不提供输入控件时，显式点击「填入」会尝试视觉兼容路径：复核窗口、输入区和标题，激活微信、点击输入区、输入文字，再用 OCR 核对。该路径需要屏幕录制及辅助功能权限，会移动鼠标；填入期间请勿操作键鼠或切换聊天。不会自动按发送键，也不使用剪贴板或 Cmd+V；换行和制表符转换为空格。

兼容路径读到已有草稿时停止，提示使用「复制」手动插入；辅助功能路径仍追加原有文字。窗口、焦点或会话变化时停止，画面无法确认时提示检查草稿，不自动重试。视觉边界及 OCR 都可能误判，标题检查也不是会话 ID，不能消除用户同时操作时的竞争；深色主题、多显示器与其他微信版本仍需更多验证。

## 下一步（按优先级）

1. **攒标注数据**：把误判的（尤其「催进度 vs 问进度」）记下来，微调冲 95%+
2. **区分聊天消息和分享的文章卡片**：保守过滤，风险是误杀正常消息
3. **数据库直读（现在是默认）的易用性**：会话挑选、大库首读慢、微信更新后重提取密钥，都需要实测反馈；想先回到读屏就把 `JEV_SOURCE` 设成 `ocr`

## 开发者

- **贡献前必读**：[CONTRIBUTING.md](CONTRIBUTING.md)——动代码前先在 issue 认领（评论 + assignee），分层自测改哪层跑哪层
- 配置界面自测：`uv run python -B -m unittest discover -s tests`；macOS 原生窗口与按钮流程：`uv run python -B probe/settings_smoke.py`（临时配置 + 本地测试服务，不使用个人密钥）。
- 数据源相关自测：`uv run python -B probe/live_db_smoke.py`（8 项实时库冒烟，需微信在跑 + 已提取密钥，只读）、`uv run python -B probe/history_ui_smoke.py`（历史会话窗口冒烟，6 秒自动退出）、`uv run python src/wechat_keys.py`（密钥状态与下一步）。纯函数与缓存策略在 `tests/test_live_db.py`，不需要真库。
- 第三方代码：`tools/wcdb_key_tool/` 是**原样引入**的 wcdb-key-tool（MIT，见 [NOTICE.md](tools/wcdb_key_tool/NOTICE.md)，含上游提交号与 SHA256）；改它请走「重新复制 + 更新 NOTICE」，本项目自己的逻辑放 `src/wechat_keys.py`。
- 打包 `./packaging/build_app.sh`；发版 `./packaging/release.sh --publish`（脚本会在构建前后强制检查干净 worktree，再做 zip 解压回验 + SHA256 + gh release）。版本号只有 `pyproject.toml` 一处；固定图标资产在 `packaging/AppIcon.icns`；有开发者证书可加 `--sign "Developer ID Application: ..."`
- 架构一句话：默认「只读打开微信加密库 → 取最近真实历史」，退回读屏时换成「进程内抓微信窗口 → Vision OCR（只扫聊天区）」；之后都是 本地 decider-2b 出意图/风险 → LLM 并发出候选 → 本地排序 → 悬浮窗 NSPanel。抓窗口不抓屏：微信被挡住也能抓，悬浮窗不污染 OCR

## 许可与免责

MIT（见 `LICENSE`）。默认的数据库直读只读**你自己账号、你自己设备上的**聊天内容：不注入、不 hook、不写微信文件、不自动发送任何消息；它需要你自行运行内置的第三方密钥提取工具（含一次 `sudo` 重签名与密钥提取）。不提取密钥也可以直接用：会自动退回 OCR 读屏，只读你自己屏幕上看到的内容。装到别人机器上读别人的聊天记录是另一回事，本项目不为那种用法背书。微信改版可能导致识别失效，请遵守微信软件许可协议。
