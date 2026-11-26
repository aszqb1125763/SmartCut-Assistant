# 参与 SmartCut 开发

感谢参与 SmartCut 智能剪辑助手。提交代码前请先阅读 [开发与构建](docs/开发与构建.md)、[安全政策](SECURITY.md) 和 [行为准则](CODE_OF_CONDUCT.md)。

## 开发环境

项目面向 Windows 10/11，推荐 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

模型、FFmpeg、Premiere、API Key 和私人素材不应提交到仓库。个人配置保存在 `conf/settings.json`，该文件已被忽略；首次运行可以直接使用内置默认值，配置格式参考 `conf/settings.example.json`。

## 提交问题

Bug 报告请包含：

- SmartCut 版本、Windows 和 Premiere 版本；
- 使用本地模型还是 API，模型名称及剪辑保留百分比；
- 入口、音轨选择、素材时长和可复现步骤；
- 脱敏日志，以及问题发生的源时间和输出时间；
- 期望结果与实际结果。

不要上传 API Key、未获授权的素材、完整私人日志或含个人路径的配置文件。字幕、切点问题优先附 10～30 秒的最小可公开样例，或仅提供精确时间点和截图。

## 代码修改

1. 一个变更只解决一个清晰问题，避免同时重写模型、字幕和工程格式。
2. 复用现有岗位合同、时间坐标、缓存和边界判据，不在 UI 中复制业务逻辑。
3. 源素材时间、Premiere 序列时间和成片时间必须使用明确字段并在边界处转换。
4. 新功能必须补充覆盖真实风险的自动化测试；不要删除或放宽测试来隐藏失败。
5. 修改字幕时必须保留原文审计信息，禁止静默删字、概括、改数字、人称、标点或新增省略号。
6. 修改 Premiere 集成时保留原工程和备份，不接受覆盖用户工程的默认行为。

## 验证

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m unittest discover -q
node --check .\pr_bridge\js\main.js
```

与模型、FFmpeg 或 Premiere 相关的变更还要说明人工验证素材、版本、结果和未覆盖范围。编译或模拟测试通过不能写成真实剪辑效果已经验证。

## Pull Request

PR 描述应列出根因、修改文件、测试命令和结果、兼容性影响、人工验证及回滚方式。界面变化附截图；字幕或切点变化附精确时间点。提交前确认没有包含密钥、大型模型、素材、日志、构建产物或本机绝对路径。

本项目以 `LICENSE` 中的 MIT License 发布。提交贡献即表示你有权提供该内容，并同意项目按 MIT License 分发你的贡献。
