# Third-Party Notices

SmartCut 智能剪辑助手使用或可选集成多个第三方项目。下表用于发布审计，不替代各项目随附的完整许可证文本。

默认 GitHub Release ZIP 是 source-bootstrap 包：它分发本项目源码、安装器、文档和 Premiere 插件源码，不分发 `智能剪辑助手.exe`、`_internal`、`.venv`、`runtime`、`bin` 或 `models`。首次安装时，安装器在用户机器上从上游来源取得所选依赖。`LICENSES/manifest.json` 中未完成的 native-runtime 条目用于阻止未经核实的离线 EXE/DLL 包公开发布，不表示 MIT 源码或默认联网安装包不能公开。

| 组件 | 用途 | 常见许可证 |
| --- | --- | --- |
| Python | 运行时 | PSF License |
| PySide6 / Qt | 桌面界面与多媒体 | LGPLv3 / GPLv3 / 商业许可，视分发方式而定 |
| FFmpeg | 音视频探测、转码与混音 | LGPL/GPL，取决于具体构建选项 |
| faster-whisper | 语音识别封装 | MIT |
| CTranslate2 | 推理运行时 | MIT |
| OpenCV | 画面处理 | Apache-2.0 |
| NumPy | 数值计算 | BSD-3-Clause |
| PyAV | FFmpeg Python 绑定 | BSD-3-Clause |
| Hugging Face Hub | 模型下载 | Apache-2.0 |
| tokenizers | 文本分词 | Apache-2.0 |
| pypinyin | 中文拼音处理 | MIT |
| PyYAML | YAML 解析 | MIT |

语音模型、语言模型和视觉模型拥有各自的模型许可、可接受使用政策和再分发限制。发布带模型的离线包前，必须逐个确认模型页面中的许可条款。

FFmpeg 和 Qt 的实际义务取决于打包时使用的二进制、编译选项和链接方式。发行者应随发布包提供对应许可证文本、源代码获取方式或其他法定材料。

API 服务商（OpenAI、DeepSeek、阿里云、硅基流动、Moonshot、智谱、Anthropic、Google 等）不随本项目分发；使用者需自行遵守各服务条款。

其他运行依赖包括 OpenCC Python（简繁转换）、RapidOCR / ONNX Runtime（OCR 与推理）、Pillow（图像）、pyclipper 与 Shapely（OCR 几何处理）。它们各有独立授权条款，发行者应以实际安装版本随附的许可证为准。PyInstaller 只列在开发构建依赖中。

