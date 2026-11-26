# Premiere 插件

SmartCut 的 Premiere CEP 插件会读取当前序列、运行相同的智能剪辑流程，并生成可继续调整的 PR 时间轴和工程文件。

## 安装

在 SmartCut 主程序的菜单中选择“安装 / 更新 Premiere 工程插件…”，打开可视化安装器。

安装器会检测常见 Premiere 安装目录及 Windows 注册表中的自定义安装位置，并列出当前用户、系统公共目录和系统 x86 公共目录中的 CEP 插件状态。推荐选择“当前用户”，通常无需管理员权限；也可以从列表选择或点击“浏览…”指定 CEP `extensions` 文件夹。

默认位置是：

```text
%APPDATA%\Adobe\CEP\extensions\com.smartcut.prbridge
```

点击“安装 / 更新插件”后，安装器先复制到临时目录并校验每个文件的 SHA256，再备份现有 `com.smartcut.prbridge` 目录并切换到新版。其它插件不受影响。旧插件备份在所选 `extensions` 文件夹的父目录：

```text
SmartCut-CEP备份\com.smartcut.prbridge\时间戳_编号
```

备份位于 CEP 扩展扫描目录之外，避免加载重复插件。安装失败时自动恢复旧插件，日志会显示具体位置。系统目录提示权限不足时，可以改用推荐的当前用户目录。

安装器仅设置当前用户的 `HKCU\Software\Adobe\CSXS.8` 至 `CSXS.12` 的 `PlayerDebugMode=1`，用于加载此未签名 CEP 扩展。它不会自动结束 Premiere。

源码环境也可以独立打开安装器：

```powershell
.\.venv\Scripts\python.exe .\pr_bridge_installer.py
```

命令行备用方式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_pr_bridge.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_pr_bridge.ps1 -TargetRoot 'D:\AdobeCEP\extensions'
```

安装后请手动完全退出并重新启动 Premiere Pro，已打开的 CEP 面板不会自动刷新。

## 本机桥安全

SmartCut 与 CEP 面板只通过 `127.0.0.1` 通信。主程序每次启动都会生成新的随机会话令牌，写入当前 Windows 用户的 `%LOCALAPPDATA%\SmartCut\bridge-session.json`；面板自动读取并随每个请求发送，不需要用户填写，也不会写进日志或仓库。服务拒绝普通网页来源和没有令牌的请求，转写、剪辑、领取方案等有副作用的操作只接受 POST。

更新到带会话鉴权的版本后，必须同时使用本版本的“安装 / 更新 Premiere 工程插件…”更新面板并重启 Premiere。旧面板没有令牌，不能连接新服务，这是预期的安全保护。会话文件只用于当前运行实例，SmartCut 正常退出时会删除；如果异常退出，下一次启动会覆盖旧令牌。

## 版本兼容范围

插件清单允许 PR 2020（内部版本 14.0）及之后仍提供 CEP 的 Premiere 版本加载，最低 CEP/CSXS 运行时为 9.0。声明可加载范围不代表已在每个 Adobe 版本逐一实机验证；仅提供 UXP、禁用 CEP 或缺少工程保存/时间轴插入接口的环境仍不能完成此工作流。

实际执行时按接口能力检测，不只根据版本号猜测：支持 `Sequence.createCaptionTrack` 时创建两条原生字幕轨；接口缺失或建轨失败时降级为导入简繁 SRT，不阻止剪辑工程保存。

## 使用步骤

1. 启动 SmartCut，使本地桥服务运行。
2. 在 Premiere 中打开包含原素材的序列。
3. 打开“窗口 → 扩展（旧版）→ 智能剪辑桥”；部分版本菜单名为“扩展”。
4. 点击“读取序列”。
5. 设置内容保留百分比、AI 听音轨和工程音轨模式。
6. 点击完整智能剪辑按钮。

## 音轨设置

- “AI 听音轨”决定语音识别和模型判断使用哪些源音轨。
- “保留全部原轨”适合继续混音。
- “只保留所选轨”适合明确只交付某些轨道的工程。

## 输出

SmartCut 会另存工程，不直接覆盖当前原工程。输出路径和备份路径会显示在面板日志中。工程内的片段仍可移动、延长、缩短和重新组织。

工程同目录会生成 `_简体中文字幕.srt` 和 `_繁体中文字幕.srt`。现代 PR 自动创建两条原生可编辑字幕轨；旧版 PR 或字幕建轨失败时，两份 SRT 会留在项目面板，面板日志提示兼容模式，可手动拖入序列。部分成功时，已创建的字幕轨会保留。

请将 `.prproj` 和两份 SRT 保留在同一文件夹，并保留原始素材。移动到另一台电脑时一并复制，必要时在 PR 中重新链接媒体。详见[字幕与工程](字幕与工程.md)。

## 源码中的可选组件

当前主面板 `pr_bridge/js/main.js`、桥服务和可编辑工程导出链路不调用 `pr_bridge/native/SmartCutPrCommand.cs` 或 `pr_bridge/bin/smartcut_pr_command.exe`。这组文件属于历史原生菜单实验，源码克隆后无需编译该 EXE，也不把它作为插件安装必需项。正式安装需要 `CSXS/manifest.xml`、`index.html`、`js/main.js`、`js/CSInterface.js` 和 `jsx/host.jsx` 等页面脚本文件。

## 常见问题

- 面板找不到：确认安装路径、`PlayerDebugMode` 和 Premiere 是否已完全重启。
- 无法连接：先启动 SmartCut，检查 `127.0.0.1:17890` 是否被安全软件拦截。
- 音轨选择不对：重新读取序列，确认 A1/A2 编号与当前时间轴一致。
- 字幕只出现在项目面板：这是当前 PR 的字幕接口兼容降级，可手动把简繁 SRT 拖入序列；工程已正常保存。
- 更换软件或插件后仍显示旧界面：检查是否在用户和系统 CEP 目录同时安装了不同副本，并完全重启 Premiere。

