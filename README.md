# SmartCut 智能剪辑助手

当前版本：**1.1.6**。产品名称与版本的源码真源为 [product_info.py](product_info.py)。

SmartCut 是面向长视频、录播和游戏实况的 Windows 智能剪辑工具。它可以分析画面、对白、停顿和音频边界，生成可直接观看的视频，也可以把剪辑结果写入可继续编辑的 Adobe Premiere Pro 工程。

## 当前能力

- 按百分比控制内容保留量，自动清理长静音和无信息空段。
- 保护词、句、音频波形和画面帧边界，减少切半句话和吞尾音。
- 支持多音轨选择，并可决定 PR 工程保留全部原轨或仅保留所选轨。
- 本地模型默认运行；也可配置 OpenAI、DeepSeek、通义千问、硅基流动、Moonshot、智谱、Ollama/vLLM、Claude、Gemini 或自定义 OpenAI 兼容接口。
- 本地模式只需安装一个模型。选择 4B、8B 或 14B 后，字幕、分析、编剧、剪辑和导演全流程统一跟随，不会暗中启动另一档模型。
- 输出字幕文件、普通成片和可继续修改时间轴的 `.prproj` 工程。
- 字幕使用词级时间、异常窗口局部复听、术语/上下文校对和语义断句，默认“一句一个意思”、单行约 15 字；字幕失败会降级告警，不会卡住视频或 PR 工程交付。

## Windows 快速开始

推荐 Python 3.12、Windows 10/11 64 位。
默认 GitHub Release 提供“源码 + 自动安装器”ZIP，不直接携带 EXE、`_internal`、虚拟环境、FFmpeg 或模型。这样既能让接收者解压后安装使用，也避免把未经完整再分发审计的 Qt/FFmpeg 等二进制混入公开包。

1. 下载 Release ZIP 并完整解压。
2. 双击 `安装并启动.bat`。脚本会自动准备 Python、依赖、FFmpeg、当前语音模型、当前本地语言模型和 Premiere 插件，随后启动软件。没有 winget 时会从 Python 官网下载官方安装器并验证数字签名，不需要用户手动补 Python。首次安装需要联网，耗时取决于网络和所选模型。
3. 需要选择安装目录时，双击 `安装到指定目录.bat`。

也可以在 PowerShell 执行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
   ```

新安装默认使用高准确率 `large-v3` 语音模型和 8B 本地语言模型；两类模型各自跟随软件设置，并且本地语言模型始终只安装用户选择的一套。`-Check` 仅检查环境，不执行安装。

安装 Premiere 插件：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1 -InstallPremierePlugin
```

只做环境体检：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1 -Check
```

更详细的说明见 [安装指南](docs/安装指南.md)。

## 基本使用

1. 打开素材或把视频拖入队列。
2. 选择剪辑模式、内容保留百分比和 AI 要听的音轨。
3. 运行“模型剪辑”。百分比越低，内容压缩越强；长静音不计入应保留内容。
4. 普通视频会写入配置的导出目录。需要继续修改时，使用 Premiere 插件生成可编辑工程。

## 文档

- [安装指南](docs/安装指南.md)
- [Premiere 插件](docs/Premiere插件.md)
- [API 模型配置](docs/API模型.md)
- [字幕与工程](docs/字幕与工程.md)
- [故障排查](docs/故障排查.md)
- [开发与构建](docs/开发与构建.md)
- [贡献指南](CONTRIBUTING.md)
- [安全政策](SECURITY.md)
- [更新记录](CHANGELOG.md)
- [v1.1.6 发行说明](RELEASE_NOTES.md)
- [开源发布与离线包说明](OPEN_SOURCE_DECISIONS.md)

使用 AI 帮助二次开发时，可先把 [开发与构建](docs/开发与构建.md) 交给 AI；其中包含模块入口、岗位合同、时间坐标、API 适配、Premiere 通信、测试选择与可直接使用的协作提示模板。

## 隐私与密钥

默认模式在本机处理。切换到 API 模型后，模型请求所需的文本或结构化证据会发送到所选服务商。API Key 保存在本机 `conf/settings.json`，界面使用密码框且普通日志不会输出 Key；请勿提交该文件，仓库的 `.gitignore` 已默认忽略它。源码仓库只提供无密钥的 `conf/settings.example.json`，首次运行无需手工复制，保存设置时会自动创建个人配置。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -q
```

部分完整链路测试需要 FFmpeg、模型文件、Premiere 或真实素材。发布前请按 [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) 检查。默认 source-bootstrap ZIP 必须提供项目 `LICENSE`、锁定依赖、SBOM、安装说明和隐私扫描；如果发布 EXE、`_internal`、运行时、FFmpeg 或模型等离线二进制，还必须让 `LICENSES/manifest.json` 对实际载荷达到 `complete=true`。可运行 `python build_tools/generate_compliance.py` 收集当前环境中的真实材料；任何未核实项都必须继续保持未完成。

公开仓库和发行包只携带 `kb/01_*.txt` 至 `kb/10_*.txt` 的项目基础词库。本机从第三方模组 JAR 生成的大型词库可能包含私人路径且没有完整再分发授权，已默认排除；用户可从自己合法持有的模组目录本地生成。

## 授权说明

本项目自有源代码采用 [MIT License](LICENSE) 开源。MIT 允许使用、复制、修改、发布、分发、再许可和销售，但必须保留版权与许可证声明。第三方组件继续遵守各自许可证，摘要见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，完整文本由 `LICENSES/` 自动归档。

公开 GitHub 源码仓库默认不包含 EXE、`_internal`、模型、素材、日志、用户设置和工作目录。发布带第三方 DLL 的 Windows 成品包属于另一项分发工作，需要通过项目现有的第三方清单门禁；这不影响先公开 MIT 源代码。

## 版权与贡献

Copyright © 2026 瓜子神教大主教、不无聊的子枫哥

版权所有人：瓜子神教大主教、不无聊的子枫哥。

- 软件初版、创意提供：[瓜子神教大主教](https://space.bilibili.com/4445859/dynamic)
- 后续全功能完善、所有剪辑模型结构提供者：[不无聊的子枫哥](https://space.bilibili.com/263271191)
- AI辅助开发：DeepSeek、OpenAI Codex

AI 仅列为辅助开发工具，不作为版权所有人。完整声明见 [NOTICE.md](NOTICE.md)。

