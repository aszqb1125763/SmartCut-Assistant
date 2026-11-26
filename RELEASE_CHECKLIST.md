# Release Checklist

## 法务与安全

- [x] 项目根目录已有两位版权所有人确认的 MIT `LICENSE`。
- [x] 用 `build_tools/generate_compliance.py` 生成精确 `requirements.lock`、CycloneDX `SBOM.cdx.json` 和许可证清单。默认 source-bootstrap 包不携带 EXE/DLL；若发布 EXE、`_internal`、运行时、FFmpeg 或模型，则必须让清单对实际载荷达到 `complete=true`、`missing` 为空并随附所需正文；`THIRD_PARTY_NOTICES.md` 不能替代这些材料。
- [x] `NOTICE.md`、软件关于、README、开发文档、安装与发布说明、Premiere 面板保留两位版权所有人的姓名、分工和可点击链接；AI 仅署名辅助开发。
- [x] 本次 source-bootstrap 包已确认未携带 EXE、DLL、`_internal`、运行时、FFmpeg 或模型。将来若发布离线二进制，仍须重新核对实际载荷的再分发要求。
- [x] 公开源码与发行包不含 `conf/settings.json`、API Key、私人素材、日志或开发者绝对路径；仓库仅提供 `conf/settings.example.json`。
- [x] 已对公开源码与发行包运行密钥、私人路径和大文件扫描。
- [x] 发行包只含 `kb/01_*.txt` 至 `kb/10_*.txt`，不含 `mods_terms.json`、`mods_hint.txt`、`术语_全量.txt`、`术语_键值.json`、`术语_统计.txt`。
- [ ] 使用 `--with-bin` 时完成 `LICENSES/bin/MANIFEST.json`；使用 `--with-models` 时完成 `LICENSES/models/MANIFEST.json`，不得用空清单绕过。（本次未启用这两个选项。）

## 代码与测试

- [x] `python -m unittest discover -q` 完成，894 项、0 失败；干净源码仓库缺少可选 FFmpeg 附件时，真实媒体集成测试与废弃的 Premiere Extract 路径按预期跳过。
- [x] `node --check pr_bridge/js/main.js` 通过。
- [ ] 源码安装模式能启动；若发布离线二进制，再验证打包模式。
- [ ] 若发布离线二进制，EXE 与 `_internal` 来自同一次构建。
- [x] ZIP 旁的 `.sha256` 校验文件存在，并能校验下载包完整性。
- [ ] API Key 不出现在状态栏、日志、异常摘要和截图中。

## 安装

- [ ] 在干净 Windows 10/11 用户上运行 `setup_windows.ps1`。
- [ ] `启动SmartCut.bat` 能启动源码安装版；若发布离线二进制，再验证打包版。
- [ ] 无 Python、无 FFmpeg、无模型时的提示可理解。
- [x] 空 PATH、无 py、无 winget、缺关键文件的模拟测试通过，且 `-Check` 不安装或下载。
- [ ] 桌面快捷方式名称为“SmartCut 智能剪辑助手”。
- [x] 卸载/回滚说明已随发行包提供。

## 产品验证

- [ ] 单素材和批量队列完成。
- [ ] 内容保留百分比实际进入模型和最终定稿。
- [ ] 多音轨选择正确。
- [ ] 普通视频、SRT/ASS、Premiere 工程分别验证。
- [ ] Premiere 工程可继续编辑，备份未覆盖原工程。
- [ ] Premiere 插件在完全重启后正常加载。
- [ ] 本地模型和至少一个 API 提供方完成冒烟测试。

## 发布物

- [x] README、docs、更新记录和 Release 说明已更新。
- [x] 产品名、版本与 `product_info.py` 一致；本次不发布 EXE。
- [x] 发布包不含测试缓存、构建缓存、备份目录和工作目录。
- [x] 默认 source-bootstrap ZIP 不含 `智能剪辑助手.exe`、`_internal`、`.venv`、`runtime`、`bin` 或 `models`，并包含 `安装并启动.bat`、`安装到指定目录.bat`、`setup_windows.ps1`、Premiere 插件载荷和安装教程。
- [x] `make_release.py` 门禁失败后，当前版本目录、ZIP 和 SHA256 会移入 `dist/_blocked/`，相关隔离测试已通过。
- [x] 压缩包已在另一目录解压，`setup_windows.ps1 -Check` 能只读运行、正确报告缺失附件且不创建 `.venv`、`bin` 或 `models`。
- [ ] 在干净 Windows 联网完成首次安装并启动主界面。
- [ ] 记录版本号、构建时间、提交号和已知限制。
