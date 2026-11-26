# SmartCut 智能剪辑助手 v1.1.5

这是 v1.1.4 首次公开版本的 Windows 安装兼容补丁。GitHub Release 仍提供 **source-bootstrap ZIP**：包含源码、教程、自动安装器和 Premiere 插件源码，不直接携带 EXE、Python 运行时、FFmpeg、模型或用户配置。

## 本次修复

- 修复部分 Windows 电脑把批处理中的 `for %%F` 错误解析成 `%F`、从而误报安装包缺文件的问题。
- 所有批处理改为 UTF-8 无 BOM、CRLF 换行，并先切换到 UTF-8 代码页再输出中文。
- 安装、启动和 PR 插件入口支持本地磁盘及 UNC 网络共享路径；仍建议安装时先完整解压到本地磁盘。

## 安装

1. 下载 `SmartCutAssistant_v1.1.5.zip` 和同名 `.sha256` 文件。
2. 完整解压 ZIP，不要直接在压缩包内运行。
3. 双击 `安装并启动.bat`；需要自选安装位置时双击 `安装到指定目录.bat`。
4. 首次安装需要联网。安装器会准备 Python 环境、依赖、FFmpeg、所选语音/语言模型和 Premiere 插件。

PowerShell 校验下载完整性：

```powershell
Get-FileHash .\SmartCutAssistant_v1.1.5.zip -Algorithm SHA256
Get-Content .\SmartCutAssistant_v1.1.5.zip.sha256
```

两处哈希值必须一致；不一致时请删除文件并重新下载。

## 主要内容

- 用百分比控制内容保留量，清理长静音和低信息段，并保护词句、波形与画面帧边界。
- 支持多音轨选择；可输出普通视频，也可生成能继续调整时间轴的 Premiere Pro 工程。
- 生成同时间轴的简体、繁体可编辑字幕；使用词级时间、异常窗口局部复听、术语/上下文校对和语义断句，默认单行约 15 字。
- 本地 4B、8B、14B 模型全流程统一跟随用户选择；也支持常见云 API 和 OpenAI 兼容接口。
- Premiere 本机桥加入随机会话令牌、来源限制和请求大小限制。
- 包含开发、安装、API、字幕、Premiere 和故障排查文档，便于继续使用 AI 协作开发。

## 已知限制

- 当前只支持 Windows 10/11 64 位；推荐 Python 3.12。
- 首次安装会下载较大的语音模型和所选本地语言模型，耗时取决于网络与硬件；API 模式仍需本地语音识别模型。
- Premiere 原生字幕轨能力因版本而异；不支持自动建轨的版本会保留两份 SRT 到项目面板供手动拖入。
- 自动剪辑和字幕仍应在交付前人工抽查，尤其是专业术语、多人重叠说话、强背景音乐和极低音量片段。
- 本次不发布离线 EXE + `_internal` 成品包；第三方二进制清单未闭合前不会用空清单绕过门禁。

## 许可与署名

项目自有源代码采用 [MIT License](LICENSE)。第三方组件继续遵守各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 `LICENSES/`。

Copyright © 2026 瓜子神教大主教、不无聊的子枫哥

- 软件初版、创意提供：[瓜子神教大主教](https://space.bilibili.com/4445859/dynamic)
- 后续全功能完善、所有剪辑模型结构提供者：[不无聊的子枫哥](https://space.bilibili.com/263271191)
- AI 辅助开发：DeepSeek、OpenAI Codex（不是版权所有人）
