# AGENTS.md

微信悬浮窗助手（macOS）：感知微信消息 → 本地模型判意图/风险 → LLM 生成候选回复 → 悬浮窗展示/一键填入。纯只读、零封号风险是**核心原则**，任何改动不得破坏。

两种数据源，都是只读：**默认数据库直读**（`sqlcipher -readonly` 打开微信加密库，需要先用内置的 `tools/extract_wechat_keys.command` 提取密钥，那一步要 sudo + 一次微信重新登录，默认只生成密钥、不生成明文数据库快照）；**没有密钥时自动退回 OCR 读屏**（不接触微信数据文件、不要额外权限），并在启动时用一次 osascript 提示去跑提取命令。两条路径都不注入、不 hook、不写微信文件、不自动发送。

## 目录与命令

- `src/perception.py` 抓图+OCR+抽消息；`src/perception_db.py` 数据库直读感知（与 `perception.read_conversation` 同构）；`src/live_db.py` 微信运行中加密库的只读 Provider；`src/wechat_keys.py` 密钥位置/状态/提取入口；`src/message_view.py`「查看读到的消息…」窗口（纯展示，格式化函数不依赖 AppKit）；`src/history.py` 解密快照 Provider、`src/history_analyze.py` / `src/history_ui.py` 历史分析 CLI 与窗口；`src/judge.py` 本地判断（decider-2b）；`src/judge_jev.py` 云端判断（TypeSafe Jev）；`src/generate.py` 候选生成（OpenAI/Anthropic 兼容 API）；`src/hud.py` 悬浮窗+轮询主循环；`src/fill.py` 辅助功能写入；`src/styles.py` 话术；`src/userconfig.py` 配置加载（数据源解析也在这里）；`src/settings.py`/`src/settings_config.py` 原生设置窗口与写回
- `tools/wcdb_key_tool/` 是**原样引入**的第三方密钥提取工具（MIT，含 NOTICE + 上游提交号 + SHA256）；本项目自己的逻辑不写进那个文件，改动要走重新复制 + 更新 NOTICE
- 启动：`./start.command`（用户平时的方式；`Ctrl+C` 退出；参数会透传，如 `./start.command --source ocr`）。没有正式测试套件，分层自测：

  ```bash
  uv run python src/perception.py                  # 感知层（读屏，见下方 CLI 验证陷阱）
  uv run python src/judge.py "这个需求你今天跟一下"
  uv run python src/judge_zh_test.py               # 22 条意图回归——改判断层 prompt 后必须重跑
  uv run python src/generate.py --check            # 生成层凭据解析
  uv run python src/wechat_keys.py                 # 数据源 db：密钥状态 + 下一步
  uv run python -B probe/live_db_smoke.py          # live 直读 8 项冒烟（需微信在跑 + 已提取密钥）
  uv run python -B probe/history_ui_smoke.py       # 历史会话窗口冒烟（6 秒自动退出）
  ```

- 日志：`~/Library/Logs/jev-jarvis.log`，分阶段耗时（读屏/判断/生成/排序/端到端）。**刻意不含消息正文与候选文字**（用户可放心贴 issue），只在事件发生时打、不在每跳打；首次调用标注「首次」。
- 发版：版本号只有 `pyproject.toml` 一处；`./packaging/release.sh --publish` 从**干净 worktree** 构建（zip 解压回验+SHA256+gh release）；无 Apple 公证，首次打开要教右键。
- 认领协议：动任何 issue 的代码前，先按 [CONTRIBUTING.md](CONTRIBUTING.md) 完成认领三步自检 + 评论认领 + 设 assignee——多人多 AI 并行扫 issue，不认领必撞车。

## 架构与硬约束

- **纯只读**：不注入、不 hook。「填入」是唯一写动作，走辅助功能接口——**别改回剪贴板+模拟 Cmd+V**（切前台不可靠、覆盖剪贴板、失败会贴进别的应用，见 `src/fill.py` 顶部注释）。**默认的 db 路径用 `sqlcipher -readonly` 打开加密库（绝不写微信文件）；退回的 OCR 路径不接触微信数据文件**。密钥提取（sudo + 重签名 + 一次微信重新登录）只发生在用户显式运行 `tools/extract_wechat_keys.command` 时——不要在任何自动路径里触发它。
- **db 模式禁止任何周期性截屏**（这是它对用户的承诺，也直接对应菜单栏那行「正在捕捉你的屏幕」）：`_warm` 不预热 Vision、`_work_inner` 的输入框定位不走 `locate_visual_input`/`chat_signature` 视觉兜底（那两条都会 `capture_image`）。曾经漏掉后者，AX 定位失败时每秒截一次屏，用户当场从菜单栏看到「jev-chat-jarvis 正在捕捉你的屏幕」并因此以为还在用 OCR。回归在 `DbModeTests.test_db_mode_never_captures_the_screen_for_input_targeting` 与 `OutgoingTests.test_ocr_mode_still_tries_the_visual_fallback`（读屏模式必须保留兜底，否则没给辅助功能权限时「填入」直接不可用）。
- **数据源是配置，不是编译期分支**（`userconfig.perception_source`：命令行 `--source` > `JEV_SOURCE` > 旧开关 `JEV_DB_MODE=1` > **默认 `db`**）：解析与回退规则在 `wechat_keys.resolve_source`，**任何回退都要打日志说明原因**（取值写错 / 没有密钥），否则用户以为「切了没生效」。hud 只落地面板状态，`_fell_back_to_ocr` 供 main() 决定要不要弹「还没密钥」的提示。**默认从 ocr 改成 db 是刻意的**：有密钥时它明显更好（真实上文、不用截屏权限、屏外消息也能看到），没有密钥时退回等价于旧行为，所以不存在「装了不能用」的状态——别把它改回「默认 ocr、db 要显式开启」。
- **轮询**：定时器 0.25s 触发，`_next_read_ts` 门控分三档——静止（指纹相同）跳过 OCR、0.25s 一跳；**变化后先 0.45s×3 跳**（burst 下一条尽快被发现），持续再动才回 1s。**未变化帧仍要跑停稳判定**（复用 `_last_full` 缓存），否则分析永远不触发。停稳 `SETTLE_S=1.2` 是防刷屏**上限不能删**；连续 `STABLE_READS` 跳安静最早 `EARLY_SETTLE_S` 可提前开闸。最小分析间隔 `MIN_GAP_S=2.0` 不能删（预判命中路径本就免冷却）。
- **「当前打开的是哪个会话」微信查不到，别再去试别的路子**：`SessionTable` 只在**清未读**时写 `last_clear_unread_timestamp`——点开一个 unread=0 的历史会话，数据库里**没有任何变化**（实测该值仍停在几十分钟前，而刚清过未读的群永远赢），`draft`/`SessionDraft`（0 行）、prefs plist、`session_draft.mmkv` 都没有；AX 树只有窗口按钮（`AXEnhancedUserInterface` 设置返回 -25208 不支持）。所以：自动跟随（clear 时间）只覆盖「有未读的会话」，**要盯住别的只能手动钉**——`DBReader.set_pin(username)`；入口有两处（都走 `_apply_pin`）：面板右上角图钉按钮（`openSessions_` → `src/session_picker.py` 的列表窗口，`_pin_rows` 提供「自动 + 最近活跃会话」）与菜单栏「跟随会话 ▸」（`pinSession_` / `menuNeedsUpdate_`）。钉住时标题标 `· 手动`。**别把这条去掉**：没有它，用户点开无未读会话时面板必然停在别的会话上，这是产品级缺陷，不是边缘情况。回归在 `tests/test_perception_db.py` 的 `test_pin_overrides_the_clear_time_heuristic` 与 `PinSessionTests`。
- **面板上两行元信息是用户核对的唯一界面凭据**：`span`（这段上下文的起止时间 + 条数，`_window_text`）与 `trigger`（JEV 何时被触发/完成，`_trigger_text`：waiting / fired / pending / done）。改读取窗口、改判断触发时机、或改上下文条数时，必须同步它们——用户是靠这两行确认「喂了什么、什么时候真的问了模型」的（他们明确要求把这两件事放到面板上）。`_render_meta` 会在测试夹具缺行时安静跳过。
- **上下文条数**（`_context_budget` / `_context_for`）：数据库直读默认把**读到的全部真实历史**喂给判断与生成（覆盖 `JUDGE_TURNS=2` / `CONTEXT_TURNS=8` 的省 prefill 默认），这正是它相对读屏的价值；`JEV_CONTEXT_MESSAGES=N` 显式指定条数，旧的 `JEV_DB_HISTORY=1` 是它的别名（早期实验开关，代码曾被删掉过一次、env 里的配置成了死键，别再删），上限 400。改动这里要连带更新日志里的「上下文 M 条」——那是用户核对的唯一凭证。判断层 prompt「别瘦身」那条注释针对的是**读屏**的短上下文，db 模式的高 prefill 是有意为之（实测 100 条 ≈ 4.3k 字符 / ~2.1k tokens）。
- **预判+生成都早跑**（`_prejudge_loop` / `_pregen_loop`，同款 latest-wins 槽位）：消息一出现两个半边同时起跑，停稳门只消费「文本仍是最新」的结果；生成结果还要话术匹配（`_take_pregen`），迟到/过期结果由 `applyCandidates_` 的话术守卫挡掉。候选**先上屏再排序**（prob=None 显示「排序中」，`_rank_payload` 完成后原位重排）。
- **分析在独立线程**（`_run_analysis` + `_analyzing` 防重入），别塞回 tick 线程——那会重新造成分析期间轮询停摆。
- **YOLO 检测框**（`_build_overlay`/`applyBoxes_`，`JEV_BOXES=1` 启动即开、菜单栏可切、默认关）：透明点击穿透窗把最近一次 OCR 的消息画成检测框，纯视觉层——窗口 ID 抓图看不见它、不参与任何管线逻辑；坐标映射依赖 1x nominal 采集尺寸=窗口点尺寸（`capture_image(nominal=True)` 成立）。`Message` 的 x/w 是框几何，折行时在 `extract_messages` 里维护。
- **本地推理用 float16**：MPS 对 bfloat16 算子覆盖不全会走慢路径（实测 ~1.4s vs ~0.75s，准确率不变）。
- **OCR 用 Vision**：语言只留 `zh-Hans`（多加 en-US 逐块一致却慢 30%）、Accurate 档（Fast 漏字）、语言校正开着、别缩 ROI（丢上下文）。**采集分辨率降到 1x**（`kCGWindowImageNominalResolution`）是实测过的例外：合成中文 6 行 2x ~140ms → 1x ~100ms、逐字一致；布局常量全是归一化的，不受影响。
- **HTTP 走 keep-alive 池**（`generate.py` 的 `http_post_json`，judge_jev 共用）：每次 urllib.urlopen 新建 DNS+TCP+TLS 白付 ~0.1–0.3 s。网络异常换新连接重试一次；>=300 按 `urllib.error.HTTPError` 形状抛（调用方 `e.read()` 拿正文），不跟随重定向。
- **配置只有 env 一种格式**（无 config.json）：`~/.config/jev-jarvis/env` 等，**凭据解析以 key 为准**——提供 key 的来源同时决定端点和模型。不提供第二种配置文件格式是有意为之。
- 两种启动方式（`start.command` / `.app`）必须同 Python 3.12（包跟 `.python-version` 走）；`.app` 是「启动器包」（不冻结 torch，首次启动固定版本 `uv 0.12.18` 建 venv）。uv 安装器来自 Astral 不可变 Release，必须先匹配内置 SHA256，失败时不得执行或回退到未固定的 Homebrew 版本。`.app` 启动脚本还会用随包 `uv.lock` 的 SHA256 戳（`~/Library/Application Support/jev-jarvis/uv.lock.sha256`）对账已有 venv，**戳不对只补依赖、不 `rm -rf` 环境**——uv 不会因为 lock 变了就补新依赖，少了这条「新包 + 老 venv」会静默缺包。回归在 `probe/bootstrap_regression.py`。
- `.app` 启动脚本按 `${XDG_CONFIG_HOME:-$HOME/.config}/jev-jarvis/env` 读配置，必须与设置窗口写入的位置（`userconfig.config_dirs()` 的第一项）一致，否则界面里改了不生效；回归在 `ConfigPathRegression`。

## 已知的坑

- **外部命令依赖，分清哪个是必需的**：**读取**必须 `sqlcipher`（`brew install sqlcipher`，`live_db._resolve_sqlcipher`）——最容易被漏，因为上游提取工具自己解密、不需要它；`wechat_keys.sqlcipher_path()` 启动即检查，缺了就退回读屏并写出安装命令，别让它以 FileNotFoundError 出现在面板上。**提取**要 `sudo`（重签名 + task_for_pid）；`lldb` 只在**第四级**（微信 4.1.10+ 首次抓 passphrase）才需要——上游是 已缓存密钥 → 已缓存 passphrase+PBKDF2 → 扫内存 raw key（4.0.x）→ LLDB 断点 四级路径，所以 `lldb_path()`/`passphrase_cached()` 只用于提示，**不要写成硬拦**（`/usr/bin/lldb` 在没装开发者工具时只是个报错的 shim，`which()` 判不出来，必须跑 `xcrun -f lldb`）。上游 README 的 Install 段与脚本 docstring 都写了 `xcode-select --install`，Usage 段没重复。
- **CLI 进程里验不了感知层**：独立 shell 进程里 `CGWindowListCreateImage` 会被拒（静默退子进程路径、无指纹）。验证要么用合成 CGImage 测纯函数，要么起真应用看日志。
- **数据库直读（`src/live_db.py`）踩过的四个坑，改这块前先看**：①`PRAGMA key = "x'<hex>'";` 的分号必须在双引号**外**；②消息库 `Name2Id` 的列是 **`user_name`**，旧变体才回退 `username`；③`_tables_of` 的**成功空结果**缓存 60s 并并发扫，`LiveQueryError` 绝不能缓存成空库；④图片只可在 `attach/<md5(username)>` 会话子树内按时间匹配，目录不存在就显示占位，禁止全树扫描猜测。
- **大库读取有成本**：公众号库的 WAL 可以到几百 MB，`messages()` 单次几秒；别在 tick 线程做首读，也别把 `conversations(with_counts=True)` 放进 UI 首屏（逐会话一次子进程，`history_ui` 在 live 模式下故意不传）。
- 坐标系：本模块布局常量（`CHAT_PANE_X_MIN` 等）是**底部原点**（Vision 口径）；`CGImageCreateWithImageInRect` 是**左上原点**，换算别搞反。
- 生成层**不能用 thinking 模型**（思考吃光 `max_tokens`，候选 0 条，面板只报「生成失败」误导用户）。
- README 实测数字皆有口径：意图 86.4% 是**无上下文**回归口径，改判断层 prompt 后别直接引用，要重跑 `judge_zh_test.py`；判断耗时引用应用内实测（~1s），不是 benchmark 的 0.75s。`judge_zh_test.py` 直接 import `judge.INTENTS`，测的就是线上 prompt。
- **判断层 prompt 描述别瘦身**：两轮压缩措辞（保语义锚点）实测 81.8% / 77.3%，低于原文 86.4%——批评/要解释的边界对措辞极敏感，省的那点 prefill 时间又藏在停稳窗口里，不划算（见 `judge.py` INTENTS 上的注释）。
- `.gitignore` 忽略全部 png 只放行 `docs/**`；新图片必须进 docs/。
- AppKit 控件宽度要渲染成 PNG 实测，`cellSize()` 会谎报。
- **滚动到末尾不是一行 `scrollToEndOfDocument_` 的事**：文本视图要 `setVerticallyResizable_(True)` + 容器 `setHeightTracksTextView_(False)`，滚之前先 `ensureLayoutForTextContainer_`，否则「底部」是布局长开之前的底部（实测停在中段 183/1424）。两个历史窗口（`src/message_view.py`、`src/history_ui.py` 的会话内容）都要打开即停在最新一条——消息是时间正序，末尾才是「现在」；分析结果反过来，仍从顶部读。回归：`probe/message_view_smoke.py`、`probe/history_ui_smoke.py` 会打印可见起点与文档末尾对比。
- 判断模型冷启动 10–20s 是已知问题（见 issue #1 预热方案）；启动后第一条慢是正常现象，别误判成回归。
- `JEV_DB_HISTORY` / `JEV_DB_MODE` 这类「代码删了、用户 env 里还留着」的键：接手时先 `grep` 一遍确认有没有人读，别当它不存在（`_db_history_text` 就是这么消失的：方法没了、配置还在、用户以为功能还在跑）。旧键能兼容就兼容，不能就明说。
- 改动用户可见行为要同步 README；待办与已定方案看 GitHub issues 和 README「下一步」。
