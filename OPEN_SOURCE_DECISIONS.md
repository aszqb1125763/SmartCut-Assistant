# GitHub 开源发布决定与离线成品包待办

两位版权所有人已共同确认：项目自有源代码采用 MIT License。根目录已经加入正式 `LICENSE`，源码可以按 MIT 公开到 GitHub。MIT 同时允许第三方商用、修改、分发和闭源二次开发，使用者必须保留版权与许可证声明。

## 2026-11-26 自动收尾状态（1.1.4）

已完成：

- 已生成 `requirements.lock`、CycloneDX `SBOM.cdx.json` 和 `LICENSES/manifest.json`，登记 62 个组件；无法核实的材料保持 `complete=false`。
- 公开包只允许携带项目自编的 10 份基础词库；含本机路径、来自第三方模组语言文件的派生大词库已从 Git/发行清单排除。
- 发布脚本会扫描绝对路径、常见密钥和私钥；门禁失败会把旧目录、ZIP 与校验文件移入 `dist/_blocked/`。
- 标准公开包不含模型和独立 FFmpeg；`--with-models`、`--with-bin` 没有独立完整许可证清单时会失败。
- 项目自有源码已经两位版权所有人授权按 MIT 公开；1.1.4 内部 Windows 构建、Premiere 插件和字幕修复可用，但这不等于已经补齐第三方二进制的离线再分发条件。
- 默认 GitHub Release 采用 source-bootstrap ZIP：携带源码、自动安装器、Premiere 插件源码、教程和基础词库，不携带 `智能剪辑助手.exe`、`_internal`、`.venv`、`runtime`、`bin` 或 `models`。接收者解压后双击安装器，首次运行联网从上游来源安装依赖和附件。

当前仍阻止“离线 EXE + `_internal`”公开发布的第三方材料：Qt 最终模块逐模块声明、PyAV 所链接 FFmpeg 的构建与源码提供方式、CTranslate2 wheel 中 Intel OpenMP 等二进制声明、Microsoft Visual C++ Runtime 再分发来源、当前 SQLite DLL 的实际来源。精确列表以 `LICENSES/manifest.json` 的 `missing` 为准。这些缺口不阻止默认 source-bootstrap ZIP 发布，也不得为了让离线包过门禁而伪造 `complete=true`。

## 1. 项目许可证（已决定）

- 项目自有源代码：MIT License。
- 版权所有人：瓜子神教大主教、不无聊的子枫哥。
- 允许商用、修改、分发和闭源二次开发，要求保留版权与 MIT 许可证。
- DeepSeek、OpenAI Codex 仅作为辅助开发工具列入致谢，不是版权所有人。

## 2. GitHub 发布范围（已确定）

- 默认 Release 发布 source-bootstrap ZIP，目标体验是“下载 ZIP → 解压 → 双击安装并启动 → 自动安装依赖与 PR 插件 → 启动软件”。
- 默认 ZIP 不直接分发 EXE、`_internal`、FFmpeg/llama 二进制、Python 运行时、虚拟环境或模型权重。
- 源码仓库排除模型、素材、日志、私人配置、API Key、构建缓存、EXE 和个人工作目录。
- 将来若另行发布完全离线包，必须按实际载荷重新完成许可证、来源、哈希、源码提供义务和干净机验证；它不是默认发布物。

## 3. 第三方许可证

发布 EXE 前需要生成完整 `LICENSES/`，覆盖实际打包组件，包括 Qt/PySide6、CTranslate2、faster-whisper、ONNX Runtime、OpenCV、PyAV/FFmpeg、Pillow、OpenCC、Shapely 等。`THIRD_PARTY_NOTICES.md` 只是摘要，不能替代各依赖要求随附的许可证文本。

模型权重、FFmpeg 和其他可选大型二进制必须分别核对来源、版本和再分发条款；无法确认时不放入公开发行包，改由用户安装器从官方来源获取。

## 4. 发布门禁

GitHub source-bootstrap 发行状态：

1. [x] 已加入正式 MIT `LICENSE`，并同步 README、NOTICE、软件关于与 Premiere 面板署名。
2. [x] 已生成锁定依赖、第三方许可证归档、SBOM 和发布包 SHA256，并完成密钥、私人路径与大文件扫描。
3. [x] 公开包排除其他项目的 `.venv`、`runtime`、`bin`、`models`、Junction、素材、日志和个人配置。
4. [x] 全量自动化测试和 Premiere 面板 JavaScript 语法检查通过。
5. [ ] 在干净 Windows 10/11 机器完成首次联网安装和真实 Premiere 版本矩阵人工验收。
6. [ ] 初始化 Git、建立首个可审查提交、创建 GitHub 仓库并上传 Release。（由发布流程最后执行。）

逐项状态与仍需人工完成的产品验收见 [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)。
