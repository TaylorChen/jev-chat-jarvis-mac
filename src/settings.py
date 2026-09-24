"""Native model settings, opened from the HUD menu. Saving requires a restart."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import AppKit as A
import objc
from Foundation import NSObject, NSMakeRect

sys_path = str(Path(__file__).parent)
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

import userconfig  # noqa: E402
import settings_config as config  # noqa: E402
import wechat_keys  # noqa: E402


class SettingsController(NSObject):
    @objc.python_method
    def build(self):
        self.path = userconfig.env_files()[0]
        self.original = config.read_document(self.path)
        values = userconfig.parse_env_file(self.path)
        self.file_values = values
        self.initial = {}
        self.fields = {}
        self.controls = []
        self.busy = False
        self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 760, 600),
            A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable,
            A.NSBackingStoreBuffered, False)
        self.window.setAppearance_(A.NSAppearance.appearanceNamed_(A.NSAppearanceNameAqua))
        self.window.setTitle_("模型设置 · 保存后重启生效")
        # The HUD and OCR overlay float above normal windows; settings must sit above both.
        self.window.setLevel_(A.NSFloatingWindowLevel + 1)
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        view = self.window.contentView()
        self.label(view, "模型设置", 24, 548, 710, 30, 22)
        restart_box = A.NSBox.alloc().initWithFrame_(NSMakeRect(24, 512, 710, 32))
        restart_box.setBoxType_(A.NSBoxCustom)
        restart_box.setBorderType_(A.NSNoBorder)
        restart_box.setCornerRadius_(5)
        restart_box.setFillColor_(A.NSColor.colorWithCalibratedRed_green_blue_alpha_(1, 0.94, 0.82, 1))
        view.addSubview_(restart_box)
        restart_notice = self.label(view, "保存后请退出应用并重启",
                                    36, 515.5, 686, 22, 15)
        restart_notice.setFont_(A.NSFont.boldSystemFontOfSize_(15))
        restart_notice.setTextColor_(A.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.25, 0.02, 1))
        self.label(view, "编辑文件：" + str(self.path).replace(str(Path.home()), "~"),
                   24, 476, 710, 34, 12)
        self.tabs = A.NSTabView.alloc().initWithFrame_(NSMakeRect(16, 130, 728, 342))
        titles = ("判断 · Jev", "生成 · OpenAI 兼容", "生成 · Anthropic 兼容")
        for index, (prefix, title) in enumerate(zip(config.PREFIXES, titles)):
            item = A.NSTabViewItem.alloc().initWithIdentifier_(prefix)
            item.setLabel_(title)
            panel = A.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 690, 300))
            summary, source = self.current_source(prefix)
            badge = self.label(panel, summary, 14, 260, 666, 26, 14)
            badge.setFont_(A.NSFont.boldSystemFontOfSize_(14))
            badge.setTextColor_(A.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.32, 0.70, 1))
            self.label(panel, source, 14, 206, 666, 48, 12)
            fields = {}
            for name, label, y in (("API_KEY", "密钥", 172), ("BASE_URL", "服务地址", 128), ("MODEL", "模型", 84)):
                self.label(panel, label, 14, y, 88, 26)
                cls = A.NSSecureTextField if name == "API_KEY" else A.NSComboBox if name == "MODEL" else A.NSTextField
                field = cls.alloc().initWithFrame_(NSMakeRect(104, y, 574, 26))
                default = "" if name == "API_KEY" else config.DEFAULTS[prefix][name == "MODEL"]
                value = values.get(f"{prefix}_{name}", default)
                if name == "API_KEY" and ("$(" in value or "`" in value):
                    value = ""  # Do not evaluate or rewrite shell/keychain expressions.
                    field.setToolTip_("此密钥由 shell 表达式提供；留空保留原行，输入新密钥才会替换。")
                field.setStringValue_(value)
                field.setFont_(A.NSFont.systemFontOfSize_(13))
                field.setDelegate_(self)
                field.setAccessibilityLabel_(title + " " + label)
                if name == "API_KEY":
                    field.setPlaceholderString_("由 shell 表达式提供：留空保留原行，输入新密钥才替换"
                                               if "$(" in values.get(f"{prefix}_{name}", "") or "`" in values.get(f"{prefix}_{name}", "")
                                               else "仅显示此文件中的密钥；不会复制环境变量中的密钥")
                if name == "MODEL":
                    self.set_models(field, [])
                    field.setCompletes_(False)
                    field.setPlaceholderString_("获取模型列表后选择，或手动填写模型名称")
                panel.addSubview_(field)
                fields[name] = field
                self.initial[f"{prefix}_{name}"] = value
                self.controls.append(field)
            self.fields[prefix] = fields
            # 「获取模型列表」lists whatever /models returns, thinking models included, and
            # a thinking model produces zero candidates here (generate.THINKING_ONLY_HINT).
            # 测试连接 already catches it, but nothing stops a user from picking one from the
            # dropdown and saving without testing — which is exactly how it gets hit. The
            # judgment tab is exempt: it has no reply-length cap to fill up.
            if prefix != "TYPESAFE":
                warn = self.label(panel, "⚠️ 生成层别用思考模型：思考占满输出长度，候选为 0"
                                  + ("；DeepSeek 用 deepseek-chat" if prefix == "OPENAI" else ""),
                                  14, 65, 666, 19, 12)
                warn.setTextColor_(A.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.25, 0.02, 1))
            hint = ("Jev 地址不含 /v1；列表接口不可用时，可手填模型。" if prefix == "TYPESAFE"
                    else "可手填模型。Ollama 地址通常含 /v1，密钥可填 ollama。" if prefix == "OPENAI"
                    else "使用 Anthropic 消息接口，支持自定义兼容服务地址。")
            self.label(panel, hint, 14, 46, 666, 19, 12)
            for text, action, x in (("获取模型列表", "fetchModels:", 370), ("测试连接", "testConnection:", 532)):
                button = self.button(panel, text, action, x, 4, 150)
                button.setTag_(index)
                self.controls.append(button)
            item.setView_(panel)
            self.tabs.addTabViewItem_(item)
        self._build_source_tab(values)
        view.addSubview_(self.tabs)
        self.label(view, "优先级：环境变量 > 用户 env > 项目 .env > 内置；两组生成密钥同时存在时 OpenAI 优先。\n清空此文件的密钥不屏蔽其他来源；切换服务需清除原来源中的优先密钥。", 24, 82, 710, 44, 12)
        self.status = self.label(view, "测试会发送固定问候语，不读取微信内容；可能产生少量服务费用。", 24, 36, 535, 42, 12)
        self.set_status(self.status.stringValue())
        self.save_button = self.button(view, "保存配置", "saveSettings:", 602, 38, 134)
        self.controls.append(self.save_button)
        self.window.center()
        return self

    @objc.python_method
    def set_status(self, text, kind="info"):
        colors = {"info": A.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.32, 0.70, 1),
                  "success": A.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.0, 0.40, 0.20, 1),
                  "error": A.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.75, 0.12, 0.12, 1)}
        self.status.setStringValue_(text)
        self.status.setTextColor_(colors[kind])
        self.status.setFont_(A.NSFont.boldSystemFontOfSize_(13))

    @objc.python_method
    def set_models(self, combo, models):
        current = combo.stringValue()
        combo.removeAllItems()
        combo.addItemsWithObjectValues_(models or ["暂无"])
        combo.setStringValue_(current)

    def comboBoxWillPopUp_(self, notification):
        self.model_before_popup = notification.object().stringValue()

    def comboBoxSelectionDidChange_(self, notification):
        combo = notification.object()
        if list(combo.objectValues()) == ["暂无"]:
            combo.deselectItemAtIndex_(0)
            combo.setStringValue_(getattr(self, "model_before_popup", ""))
        else:
            self.set_status("模型已修改，请重新测试；保存后重启生效。")

    @objc.python_method
    def current_source(self, prefix):
        if prefix == "TYPESAFE":
            source = userconfig.source_of("TYPESAFE_API_KEY", "JEV_API_KEY")
            summary = ("本次启动：正在使用自己的 Jev 密钥" if source != "none"
                       else "本次启动：正在使用本地判断模型，未使用 Jev 密钥")
        else:
            oai = userconfig.provider("OPENAI")
            anth = userconfig.provider("ANTHROPIC")
            selected = "OPENAI" if oai["key"] else "ANTHROPIC" if anth["key"] else None
            if selected:
                name = "OpenAI 兼容" if selected == "OPENAI" else "Anthropic 兼容"
                summary = "本次启动：正在使用自己的密钥（" + name + "）"
                source = (oai if selected == "OPENAI" else anth)["source"]
                if selected != prefix:
                    source += "；本页服务当前未启用"
            else:
                summary = "本次启动：未配置生成密钥"
                source = "none"
        detail = "来源：" + source.replace(str(Path.home()), "~") + "\n以下编辑内容保存后，需重启应用才会生效。"
        return summary, detail

    @objc.python_method
    def _build_source_tab(self, values):
        """数据源标签页：OCR 读屏（默认）还是数据库直读，以及后者的前提条件。

        单独一个标签页而不是塞进模型页：它决定的是「消息从哪来」，和模型凭据无关；
        而且切到 db 需要额外的密钥提取（要 sudo 和一次微信重新登录），必须写在
        用户能看见的地方，而不是等启动日志里回退。
        """
        item = A.NSTabViewItem.alloc().initWithIdentifier_("SOURCE")
        item.setLabel_("感知 · 数据源")
        panel = A.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 690, 300))

        # 选中态来自配置文件；文件没写就是默认数据源（db）。顶部那行「本次启动」说
        # 的是正在跑的那个（可能来自环境变量或 --source），两者不同是正常的。
        current = config.validate_source(values.get("JEV_SOURCE")
                                         or userconfig.DEFAULT_PERCEPTION_SOURCE)
        running = userconfig.perception_source()
        self.source_seg = A.NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(14, 252, 360, 28))
        self.source_seg.setSegmentCount_(2)
        for i, label in enumerate(("OCR 读屏", "数据库直读（默认）")):
            self.source_seg.setLabel_forSegment_(label, i)
        self.source_seg.setSelectedSegment_(1 if current == "db" else 0)
        self.source_seg.setTarget_(self)
        self.source_seg.setAction_("sourceChanged:")
        self.source_seg.setAccessibilityLabel_("数据源")
        panel.addSubview_(self.source_seg)
        self.controls.append(self.source_seg)

        if running == "db":
            summary = "本次启动：数据库直读（消息来自微信本地库，不读屏）"
        else:
            summary = "本次启动：OCR 读屏（截取微信窗口 + Vision 识别）"
        badge = self.label(panel, summary, 14, 224, 666, 24, 14)
        badge.setFont_(A.NSFont.boldSystemFontOfSize_(14))
        badge.setTextColor_(A.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.32, 0.70, 1))
        self.label(panel, "数据库直读（默认）：只读打开微信自己的加密库，消息更全（带真实上文、不漏屏外消息），"
                          "没有密钥时会自动退回读屏。\n"
                          "需要先提取密钥——要 sudo 给微信 ad-hoc 重签名（去掉 Hardened Runtime），"
                          "首次还要在微信里退出登录再登录一次。\n"
                          "OCR 读屏：不需要额外权限，不接触微信数据文件。\n"
                          "两条路径都不注入、不 hook、不自动发送消息；填入仍是唯一的写动作。",
                   14, 138, 666, 82, 12)

        self.label(panel, "密钥文件（留空用默认位置）", 14, 118, 200, 22, 12)
        self.keys_field = A.NSTextField.alloc().initWithFrame_(NSMakeRect(220, 116, 458, 26))
        self.keys_field.setStringValue_(values.get("JEV_KEYS_FILE", ""))
        self.keys_field.setPlaceholderString_(str(wechat_keys.keys_file()))
        self.keys_field.setFont_(A.NSFont.systemFontOfSize_(13))
        self.keys_field.setDelegate_(self)
        self.keys_field.setAccessibilityLabel_("微信密钥文件")
        panel.addSubview_(self.keys_field)
        self.controls.append(self.keys_field)

        self.key_status = self.label(panel, wechat_keys.status_line(), 14, 82, 666, 24, 12)
        self.key_status.setTextColor_(A.NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.0 if wechat_keys.has_keys() else 0.55, 0.35 if wechat_keys.has_keys() else 0.25, 0.10, 1))
        button = self.button(panel, "在终端里提取密钥…", "extractKeys:", 14, 34, 210)
        self.controls.append(button)
        self.label(panel, "会在终端里先重签名微信、再提取密钥；不会生成明文数据库快照。",
                   234, 38, 444, 34, 12)

        item.setView_(panel)
        self.tabs.addTabViewItem_(item)
        self.source_initial = {"JEV_SOURCE": current,
                               "JEV_KEYS_FILE": values.get("JEV_KEYS_FILE", "")}

    @objc.python_method
    def source_values(self):
        return {"JEV_SOURCE": "db" if self.source_seg.selectedSegment() == 1 else "ocr",
                "JEV_KEYS_FILE": str(self.keys_field.stringValue()).strip()}

    def sourceChanged_(self, sender):
        self.set_status("数据源已修改，保存后重启生效。")

    def extractKeys_(self, sender):
        """在终端里跑提取脚本：它需要 sudo 交互，不适合塞进本进程。"""
        try:
            subprocess.Popen(["open", "-a", "Terminal", str(wechat_keys.EXTRACT_COMMAND)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.set_status(f"打不开终端，请手动运行：{wechat_keys.EXTRACT_COMMAND}", "error")
            return
        self.set_status("已在终端打开提取脚本；完成后回到这里点保存，并重启应用。")

    @objc.python_method
    def label(self, view, text, x, y, w, h, size=13):
        field = A.NSTextField.wrappingLabelWithString_(text)
        field.setFrame_(NSMakeRect(x, y, w, h))
        field.setFont_(A.NSFont.systemFontOfSize_(size))
        view.addSubview_(field)
        return field

    @objc.python_method
    def button(self, view, title, action, x, y, width):
        button = A.NSButton.alloc().initWithFrame_(NSMakeRect(x, y, width, 32))
        button.setTitle_(title)
        button.setBezelStyle_(A.NSBezelStyleRounded)
        button.setTarget_(self)
        button.setAction_(action)
        view.addSubview_(button)
        return button

    @objc.python_method
    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        A.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.python_method
    def values(self, prefix):
        return {k: str(v.stringValue()) for k, v in self.fields[prefix].items()}

    @objc.python_method
    def changed(self):
        changed = {f"{p}_{k}": v for p in config.PREFIXES for k, v in self.values(p).items()
                   if v != self.initial[f"{p}_{k}"]}
        changed.update({k: v for k, v in self.source_values().items()
                        if v != self.source_initial.get(k)})
        return changed

    def controlTextDidChange_(self, notification):
        field = notification.object()
        for fields in self.fields.values():
            if field in (fields["API_KEY"], fields["BASE_URL"]):
                combo = fields["MODEL"]
                self.set_models(combo, [])
        self.set_status("配置已修改，请重新测试；保存后重启生效。")

    def saveSettings_(self, sender):
        self.window.makeFirstResponder_(None)
        changes = self.changed()
        if not changes:
            self.set_status("没有需要保存的修改。")
            return
        # Persist missing displayed defaults for edited services, but keep untouched key lines.
        for prefix in config.PREFIXES:
            if any(k.startswith(prefix + "_") for k in changes):
                changes.update({f"{prefix}_{k}": v for k, v in self.values(prefix).items()
                                if k != "API_KEY" and f"{prefix}_{k}" not in self.file_values})
        try:
            for prefix in config.PREFIXES:
                if any(k.startswith(prefix + "_") for k in changes):
                    vals = self.values(prefix)
                    if vals["API_KEY"] and (not vals["BASE_URL"].strip() or not vals["MODEL"].strip()):
                        raise ValueError("填写密钥后，请同时填写该服务的地址和模型。")
            for key, value in changes.items():
                if key.endswith("_BASE_URL") and value:
                    config.validate_endpoint(value)
            self.original = config.write_settings(self.path, self.original, changes)
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        except OSError:
            self.set_status("保存失败：请检查文件权限及可用磁盘空间。", "error")
            return
        self.initial.update(changes)
        self.file_values.update(changes)
        self.source_initial.update({k: v for k, v in changes.items()
                                    if k in config.EXTRA_KEYS})
        self.key_status.setStringValue_(wechat_keys.status_line())
        self.set_status("已保存。请退出并重新打开应用；当前会话继续使用启动时的配置。", "success")

    def fetchModels_(self, sender):
        self.start_request(sender.tag(), True)

    def testConnection_(self, sender):
        self.start_request(sender.tag(), False)

    @objc.python_method
    def start_request(self, index, listing):
        if self.busy:
            return
        self.window.makeFirstResponder_(None)
        prefix = config.PREFIXES[index]
        values = self.values(prefix)
        try:
            config.validate_endpoint(values["BASE_URL"])
            if not values["API_KEY"]:
                raise ValueError("请填写密钥；Ollama 可填写 ollama。")
            if not listing and not values["MODEL"].strip():
                raise ValueError("请填写模型后再测试。")
            extra = None
            if not listing and prefix == "OPENAI":
                # Match generation's current extra-body setting, without changing it.
                raw = userconfig.get("OPENAI_EXTRA_BODY")
                extra = json.loads(raw) if raw else {}
                if not isinstance(extra, dict):
                    raise ValueError("OPENAI_EXTRA_BODY 必须是 JSON 对象。")
        except json.JSONDecodeError:
            self.set_status("OPENAI_EXTRA_BODY 不是有效 JSON，请先修正该配置。", "error")
            return
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        if listing:
            combo = self.fields[prefix]["MODEL"]
            self.set_models(combo, [])
        self.busy = True
        for control in self.controls:
            control.setEnabled_(False)
        self.set_status("正在获取模型列表…" if listing else "正在测试所填服务与模型…")

        def work():
            result = {"index": index, "listing": listing}
            try:
                args = (prefix, values["BASE_URL"], values["API_KEY"])
                if listing:
                    result["models"] = config.list_models(*args)
                else:
                    config.test_connection(*args, values["MODEL"], extra)
            except Exception as e:
                result["error"] = config.error_message(e)
            self.performSelectorOnMainThread_withObject_waitUntilDone_("requestFinished:", result, False)
        threading.Thread(target=work, daemon=True).start()

    def requestFinished_(self, result):
        self.busy = False
        for control in self.controls:
            control.setEnabled_(True)
        if result.get("error"):
            self.set_status(result["error"] + (" 模型仍可手填。" if result["listing"] else ""), "error")
        elif result["listing"]:
            combo = self.fields[config.PREFIXES[result["index"]]]["MODEL"]
            self.set_models(combo, result["models"])
            self.set_status(f"已获取 {len(result['models'])} 个模型。请从下拉列表选择或手填，再测试连接。", "success")
        else:
            self.set_status("连接成功：所填服务与模型返回了有效结果。配置尚需保存并重启生效。", "success")

    def windowShouldClose_(self, sender):
        if self.busy:
            self.set_status("请求进行中，请等待结果后关闭。")
            return False
        if self.changed():
            alert = A.NSAlert.alloc().init()
            alert.setMessageText_("放弃尚未保存的配置？")
            alert.addButtonWithTitle_("继续编辑")
            alert.addButtonWithTitle_("放弃修改")
            return alert.runModal() == A.NSAlertSecondButtonReturn
        return True


if __name__ == "__main__":
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
    userconfig.load()
    controller = SettingsController.alloc().init().build()
    controller.show()
    app.run()
