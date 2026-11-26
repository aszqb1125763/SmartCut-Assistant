# -*- coding: utf-8 -*-
"""设置窗口 v2（按设计稿：左边导航 + 右边内容）。

分类：输出目录 / 剪辑 / 字幕 / 背景音乐 / 识别与模型 / 训练
读写沿用 cards_ui 的 load_settings / save_settings，键名不变，旧配置直接兼容。
"""
from __future__ import annotations

import json
import os
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QDoubleSpinBox, QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox,
    QRadioButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

import ui_theme
from product_info import PRODUCT_NAME
from ui_widgets import Panel

CATS = [("输出目录", "每段 / 合并 / 智能剪辑各存哪儿"),
        ("剪辑", "保留比例、去重、跳过导演"),
        ("字幕", "烧不烧进画面"),
        ("背景音乐", "曲库与音量"),
        ("识别与模型", "识别精度、智能剪辑模型与 API"),
        ("导演与记忆", "任务恢复、岗位状态和长期经验"),
        ("训练", "教它你的剪辑口味")]


def _settings_path():
    return os.path.join(_home(), "conf", "settings.json")


def _load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_settings(value):
    path = _settings_path()
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        return True
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _row(label: str, widget: QWidget, hint: str = "") -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 2, 0, 2)
    lay.setSpacing(10)
    lab = QLabel(label)
    lab.setFixedWidth(96)
    lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    lab.setStyleSheet("color:#a8a8a8;")
    lay.addWidget(lab)
    lay.addWidget(widget, 1)
    if hint:
        h = QLabel(hint)
        h.setStyleSheet("color:#6e6e6e;font-size:11px;")
        lay.addWidget(h)
    return box


class SettingsDialogV2(QDialog):
    def __init__(self, parent=None, initial_page=None):
        super().__init__(parent)
        self.setWindowTitle("设置 · " + PRODUCT_NAME)
        self.resize(940, 660)
        self.setMinimumSize(860, 560)
        self._cfg = _load_settings()
        self._save = _save_settings
        try:
            import memory_config
            self._memory_cfg = memory_config.load_config(home=_home())
        except Exception:
            self._memory_cfg = {}
        self._build()
        if initial_page is not None:
            self.select_page(initial_page)

    # ------------------------------------------------------------ UI
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QWidget()
        head.setFixedHeight(40)
        head.setStyleSheet("background:#181818;border-bottom:1px solid #3a3a3a;")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 0, 12, 0)
        t = QLabel("设置")
        t.setStyleSheet("font-size:14px;font-weight:600;")
        hl.addWidget(t)
        hl.addStretch(1)
        b_close = QPushButton("✕")
        b_close.setObjectName("ghost")
        b_close.setFixedHeight(26)
        b_close.clicked.connect(self.reject)
        hl.addWidget(b_close)
        root.addWidget(head)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setFixedWidth(196)
        self.nav.setStyleSheet(
            "QListWidget{background:#181818;border:none;border-right:1px solid #3a3a3a;padding:6px;}"
            "QListWidget::item{padding:9px 10px;border-radius:4px;}"
            "QListWidget::item:selected{background:#2b3a4d;color:#e6e6e6;}")
        for name, _hint in CATS:
            self.nav.addItem(name)
        self.nav.currentRowChanged.connect(self._switch)
        body.addWidget(self.nav)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(16, 12, 16, 8)
        rl.setSpacing(8)
        self.title = QLabel(CATS[0][0])
        self.title.setStyleSheet("font-size:14px;font-weight:600;")
        self.hint = QLabel(CATS[0][1])
        self.hint.setStyleSheet("color:#7d7d7d;font-size:11px;")
        rl.addWidget(self.title)
        rl.addWidget(self.hint)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_out())
        self.stack.addWidget(self._page_cut())
        self.stack.addWidget(self._page_subs())
        self.stack.addWidget(self._page_bgm())
        self.stack.addWidget(self._page_model())
        self.stack.addWidget(self._page_memory())
        self.stack.addWidget(self._page_train())
        rl.addWidget(self.stack, 1)
        body.addWidget(right, 1)
        root.addLayout(body, 1)

        foot = QWidget()
        foot.setFixedHeight(48)
        foot.setStyleSheet("border-top:1px solid #3a3a3a;")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 8, 16, 8)
        fl.addStretch(1)
        b_cancel = QPushButton("取消")
        b_cancel.setFixedHeight(30)
        b_cancel.clicked.connect(self.reject)
        b_save = QPushButton("保存")
        b_save.setObjectName("primary")
        b_save.setFixedHeight(30)
        b_save.clicked.connect(self.accept)
        fl.addWidget(b_cancel)
        fl.addWidget(b_save)
        root.addWidget(foot)

        self.nav.setCurrentRow(0)

    def _switch(self, i):
        if 0 <= i < len(CATS):
            self.title.setText(CATS[i][0])
            self.hint.setText(CATS[i][1])
            self.stack.setCurrentIndex(i)

    def select_page(self, page):
        """切换到指定设置页，供主界面的快捷入口复用。"""
        if isinstance(page, str):
            page = next((i for i, item in enumerate(CATS) if item[0] == page), -1)
        try:
            page = int(page)
        except (TypeError, ValueError):
            return False
        if not 0 <= page < len(CATS):
            return False
        self.nav.setCurrentRow(page)
        return True

    # ------------------------------------------------------------ 各页
    def _path_row(self, key, label, hint=""):
        edit = QLineEdit(self._cfg.get(key) or "")
        edit.setFixedHeight(28)
        edit.setPlaceholderText("留空 = 工作目录\\导出\\<素材名>")
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(edit, 1)
        b = QPushButton("选择…")
        b.setFixedHeight(28)
        b.clicked.connect(lambda: self._pick(edit))
        lay.addWidget(b)
        b2 = QPushButton("默认")
        b2.setObjectName("ghost")
        b2.setFixedHeight(28)
        b2.clicked.connect(lambda: edit.setText(""))
        lay.addWidget(b2)
        self._bind(key, edit)
        return _row(label, box, hint)

    def _pick(self, edit):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            edit.setText(d)

    def _bind(self, key, widget):
        setattr(self, "_w_" + key, widget)
        keys = getattr(self, "_bound_keys", None)
        if keys is None:
            keys = set()
            self._bound_keys = keys
        keys.add(key)

    def _page_out(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(_row("说明", QLabel("各类导出可以分别指定文件夹；留空就用默认。"),
                           ""))
        lay.addWidget(self._path_row("out_single", "每段导出", "每段各存一个文件"))
        lay.addWidget(self._path_row("out_merge", "合并成一条", "拼成一条成片"))
        lay.addWidget(self._path_row("out_autocut", "智能剪辑", "自动剪空段后出片"))
        lay.addStretch(1)
        return page

    def _page_cut(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        import edit_modes
        mode = QComboBox()
        for key, name, desc in edit_modes.choices():
            mode.addItem(name, key)
            mode.setItemData(mode.count() - 1, desc, Qt.ToolTipRole)
        current_mode = mode.findData(edit_modes.normalize(self._cfg.get("edit_mode")))
        mode.setCurrentIndex(max(0, current_mode))
        self._bind("edit_mode", mode)
        lay.addWidget(_row("剪辑模式", mode, "模式决定内容结构；百分比决定保留多少有效内容"))

        import cut_strength
        keep = QSpinBox()
        keep.setRange(15, 100)
        keep.setSuffix(" %")
        keep.setValue(cut_strength.percent(
            cut_strength.ratio_from_settings(self._cfg)))
        self._bind("keep_ratio_percent", keep)
        lay.addWidget(_row(
            "剪辑保留比例", keep,
            "数值越低剪得越狠；无对白、无文字信息的长空段始终清理"))

        cb_dedupe = QCheckBox("自动去重（同一段说两遍，只留更完整的一版）")
        cb_dedupe.setChecked(str(self._cfg.get("dedupe", "1")) in ("1", "true", "True"))
        self._bind("dedupe", cb_dedupe)
        lay.addWidget(cb_dedupe)

        cb_skip = QCheckBox("跳过导演读片（用已有剧本，快 2~4 分钟）")
        cb_skip.setChecked(str(self._cfg.get("skip_director", "0")) in ("1", "true", "True"))
        self._bind("skip_director", cb_skip)
        lay.addWidget(cb_skip)
        lay.addStretch(1)
        return page

    def _page_subs(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(10)
        cb = QCheckBox("出片时把字幕烧进画面")
        cb.setChecked(str(self._cfg.get("burn_subs", "0")) in ("1", "true", "True"))
        self._bind("burn_subs", cb)
        lay.addWidget(cb)
        tip = QLabel("关掉也会照样生成 _字幕.srt / _字幕.ass 放在成片旁边，B 站/抖音可直接传。")
        tip.setStyleSheet("color:#7d7d7d;font-size:11px;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        b = QPushButton("打开字幕样式窗口…")
        b.setFixedHeight(30)
        b.clicked.connect(self._open_subtitle)
        lay.addWidget(b)
        lay.addStretch(1)
        return page

    def _page_bgm(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(10)
        cb = QCheckBox("自动配 BGM")
        cb.setChecked(str(self._cfg.get("bgm", "1")) in ("1", "true", "True"))
        self._bind("bgm", cb)
        lay.addWidget(cb)

        self._bgm_conf_path = os.path.join(_home(), "conf", "bgm.json")
        try:
            import bgm
            bc = bgm.load_conf(self._bgm_conf_path)
        except Exception:  # noqa: BLE001
            bc = {"folder": "", "level_db": "-22"}

        self.bgm_folder_edit = QLineEdit(str(bc.get("folder", "") or ""))
        self.bgm_folder_edit.setFixedHeight(28)
        self.bgm_folder_edit.setPlaceholderText("选择一个存放 BGM 的文件夹")
        folder_box = QWidget()
        folder_lay = QHBoxLayout(folder_box)
        folder_lay.setContentsMargins(0, 0, 0, 0)
        folder_lay.setSpacing(6)
        folder_lay.addWidget(self.bgm_folder_edit, 1)
        pick = QPushButton("选择…")
        pick.setFixedHeight(28)
        pick.clicked.connect(self._pick_bgm_folder)
        folder_lay.addWidget(pick)
        self.bgm_count_label = QLabel("")
        self.bgm_count_label.setMinimumWidth(64)
        self.bgm_count_label.setStyleSheet("color:#7d7d7d;font-size:11px;")
        folder_lay.addWidget(self.bgm_count_label)
        lay.addWidget(_row("曲库", folder_box, "支持 mp3 / wav / flac / m4a 等"))
        self.bgm_folder_edit.textChanged.connect(self._refresh_bgm_count)
        self._refresh_bgm_count()

        self.bgm_level = QDoubleSpinBox()
        self.bgm_level.setRange(-80.0, -6.0)
        self.bgm_level.setSingleStep(1.0)
        self.bgm_level.setDecimals(0)
        self.bgm_level.setSuffix(" dB")
        self.bgm_level.setFixedWidth(100)
        try:
            level = float(bc.get("level_db", -22) or -22)
        except (TypeError, ValueError):
            level = -22.0
        self.bgm_level.setValue(level)
        lay.addWidget(_row("音量", self.bgm_level,
                           "数值越小越轻；默认 -22 dB"))
        switch_tip = QLabel("换曲：每 1～3 分钟随机一次，优先选在章节、话题切换或停顿处。")
        switch_tip.setStyleSheet("color:#7d7d7d;font-size:11px;")
        switch_tip.setWordWrap(True)
        lay.addWidget(switch_tip)
        lay.addStretch(1)
        return page

    def _pick_bgm_folder(self):
        start = self.bgm_folder_edit.text().strip()
        if start and not os.path.isdir(start):
            start = os.path.dirname(start) if os.path.dirname(start) else ""
        folder = QFileDialog.getExistingDirectory(self, "选择 BGM 曲库目录", start)
        if folder:
            self.bgm_folder_edit.setText(folder)

    def _refresh_bgm_count(self):
        folder = self.bgm_folder_edit.text().strip()
        if not folder or not os.path.isdir(folder):
            self.bgm_count_label.setText("没有音频")
            return
        try:
            import bgm
            count = len(bgm.list_tracks(folder))
            self.bgm_count_label.setText("%d 首" % count if count else "没有音频")
        except Exception:  # noqa: BLE001
            self.bgm_count_label.setText("读取失败")

    def _page_model(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)
        cb = QComboBox()
        cb.addItem("识别：准确（large-v3）", "large-v3")
        cb.addItem("识别：快（turbo，字更乱）", "large-v3-turbo")
        cb.setFixedHeight(28)
        cur = self._cfg.get("model") or "large-v3"
        for i in range(cb.count()):
            if cb.itemData(i) == cur:
                cb.setCurrentIndex(i)
        self._bind("model", cb)
        lay.addWidget(_row("语音识别", cb, "改完下次分析生效"))

        import ai_api_settings
        provider = QComboBox()
        for provider_id, label, _url in ai_api_settings.PROVIDERS:
            provider.addItem(label, provider_id)
        provider_id = ai_api_settings.normalize_provider(
            self._cfg.get("llm_provider"))
        provider.setCurrentIndex(max(0, provider.findData(provider_id)))
        self._bind("llm_provider", provider)
        lay.addWidget(_row(
            "智能剪辑模型", provider,
            "默认本地；选择 API 后只上传模型请求所需的数据"))

        base_url = QLineEdit(str(self._cfg.get("llm_api_base_url") or ""))
        base_url.setFixedHeight(28)
        base_url.setPlaceholderText("例如 https://api.example.com/v1")
        self._bind("llm_api_base_url", base_url)
        lay.addWidget(_row("接口地址", base_url, "提供方预设可修改"))

        api_key = QLineEdit(str(self._cfg.get("llm_api_key") or ""))
        api_key.setFixedHeight(28)
        api_key.setEchoMode(QLineEdit.Password)
        api_key.setPlaceholderText("服务要求时填写；不会显示在日志中")
        self._bind("llm_api_key", api_key)
        lay.addWidget(_row("API Key", api_key, "密码框保存，不写入普通日志"))

        api_model = QLineEdit(str(self._cfg.get("llm_api_model") or ""))
        api_model.setFixedHeight(28)
        api_model.setPlaceholderText("例如 gpt-4.1-mini / deepseek-chat")
        self._bind("llm_api_model", api_model)
        lay.addWidget(_row("模型名", api_model, "填写服务端实际模型 ID"))

        timeout = QSpinBox()
        timeout.setRange(ai_api_settings.MIN_TIMEOUT, ai_api_settings.MAX_TIMEOUT)
        timeout.setSuffix(" 秒")
        timeout.setValue(ai_api_settings.normalize_timeout(
            self._cfg.get("llm_api_timeout")))
        self._bind("llm_api_timeout", timeout)
        lay.addWidget(_row("请求超时", timeout, "长片分析建议 120 秒以上"))

        self._llm_last_provider = provider_id
        provider.currentIndexChanged.connect(self._sync_llm_provider)
        self._sync_llm_provider()
        cb2 = QCheckBox("生成卡片缩略图（慢：一句一张，机械盘约 9 分钟）")
        cb2.setChecked(str(self._cfg.get("thumbs", "0")) in ("1", "true", "True"))
        self._bind("thumbs", cb2)
        lay.addWidget(cb2)
        lay.addStretch(1)
        return page

    def _sync_llm_provider(self, *_args):
        """Apply provider URL presets without overwriting a custom endpoint."""
        import ai_api_settings
        provider_widget = getattr(self, "_w_llm_provider", None)
        if provider_widget is None:
            return
        provider = ai_api_settings.normalize_provider(
            provider_widget.currentData())
        previous = getattr(self, "_llm_last_provider", provider)
        base_widget = getattr(self, "_w_llm_api_base_url", None)
        if base_widget is not None:
            current_url = base_widget.text().strip()
            old_default = ai_api_settings.default_base_url(previous)
            new_default = ai_api_settings.default_base_url(provider)
            if provider != previous and (not current_url or current_url == old_default):
                base_widget.setText(new_default)
            elif provider != "local" and not current_url and new_default:
                base_widget.setText(new_default)
        enabled = ai_api_settings.is_remote(provider)
        for key in ("llm_api_base_url", "llm_api_key", "llm_api_model",
                    "llm_api_timeout"):
            widget = getattr(self, "_w_" + key, None)
            if widget is not None:
                widget.setEnabled(enabled)
        self._llm_last_provider = provider

    def _page_memory(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        self.mem_enabled = QCheckBox("启用任务工作记忆和导演长期记忆")
        self.mem_enabled.setChecked(bool(self._memory_cfg.get("enabled", True)))
        lay.addWidget(self.mem_enabled)

        self.mem_root_edit = QLineEdit(str(self._memory_cfg.get("root") or ""))
        self.mem_root_edit.setFixedHeight(28)
        self.mem_root_edit.setPlaceholderText("留空 = 工作目录\\导演记忆")
        root_box = QWidget()
        root_lay = QHBoxLayout(root_box)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(6)
        root_lay.addWidget(self.mem_root_edit, 1)
        pick = QPushButton("选择…")
        pick.setFixedHeight(28)
        pick.clicked.connect(self._pick_memory_root)
        root_lay.addWidget(pick)
        open_btn = QPushButton("打开")
        open_btn.setObjectName("ghost")
        open_btn.setFixedHeight(28)
        open_btn.clicked.connect(self._open_memory_root)
        root_lay.addWidget(open_btn)
        default_btn = QPushButton("默认")
        default_btn.setObjectName("ghost")
        default_btn.setFixedHeight(28)
        default_btn.clicked.connect(lambda: self.mem_root_edit.setText(""))
        root_lay.addWidget(default_btn)
        lay.addWidget(_row("记忆目录", root_box, "开发版和正式版各用自己的默认目录"))

        self.mem_share = QComboBox()
        self.mem_share.addItem("只共享事实和硬规则（推荐）", "facts_only")
        self.mem_share.addItem("只共享已验证经验", "validated")
        self.mem_share.addItem("完全隔离", "none")
        self.mem_share.addItem("共享全部经验（不推荐）", "all")
        share_index = self.mem_share.findData(
            str(self._memory_cfg.get("cross_mode_sharing") or "facts_only"))
        self.mem_share.setCurrentIndex(max(0, share_index))
        lay.addWidget(_row("模式间共享", self.mem_share,
                           "游戏模式与未来模式分库存放，只共享允许的经验"))

        self.mem_auto_learn = QCheckBox("导演自动复盘；从返工结果和通过验收的成片中学习")
        self.mem_auto_learn.setChecked(bool(self._memory_cfg.get("auto_learn", True)))
        lay.addWidget(self.mem_auto_learn)

        self.mem_promotion = QComboBox()
        self.mem_promotion.addItem("两个独立成片验证后晋升（推荐）", "validated_only")
        self.mem_promotion.addItem("只允许手动晋升", "manual")
        self.mem_promotion.addItem("候选经验自动晋升（风险较高）", "automatic")
        promotion_index = self.mem_promotion.findData(
            str(self._memory_cfg.get("candidate_promotion") or "validated_only"))
        self.mem_promotion.setCurrentIndex(max(0, promotion_index))
        lay.addWidget(_row("经验晋升", self.mem_promotion,
                           "先保存候选经验，成片复审奖惩；未经重复验证不会改变后续剪辑"))

        audio_box = QWidget()
        audio_lay = QHBoxLayout(audio_box)
        audio_lay.setContentsMargins(0, 0, 0, 0)
        audio_lay.setSpacing(8)
        self.mem_audio_review = QCheckBox("复听真实音频")
        self.mem_audio_review.setChecked(
            bool(self._memory_cfg.get("audio_review_enabled", True)))
        self.mem_audio_windows = QSpinBox()
        self.mem_audio_windows.setRange(4, 120)
        self.mem_audio_windows.setSuffix(" 个关键窗口")
        self.mem_audio_windows.setValue(int(
            self._memory_cfg.get("director_max_review_windows", 28)))
        audio_lay.addWidget(self.mem_audio_review)
        audio_lay.addWidget(self.mem_audio_windows)
        audio_lay.addStretch(1)
        lay.addWidget(_row("导演听觉", audio_box,
                           "文字与真实波形同为主证据；检查首字、尾音和多音轨事件"))

        visual_box = QWidget()
        visual_lay = QHBoxLayout(visual_box)
        visual_lay.setContentsMargins(0, 0, 0, 0)
        visual_lay.setSpacing(8)
        self.mem_visual_review = QCheckBox("检查关键画面")
        self.mem_visual_review.setChecked(
            bool(self._memory_cfg.get("visual_review_enabled", True)))
        self.mem_visual_nodes = QSpinBox()
        self.mem_visual_nodes.setRange(3, 60)
        self.mem_visual_nodes.setSuffix(" 个节点")
        self.mem_visual_nodes.setValue(int(
            self._memory_cfg.get("visual_max_nodes", 18)))
        visual_lay.addWidget(self.mem_visual_review)
        visual_lay.addWidget(self.mem_visual_nodes)
        visual_lay.addStretch(1)
        lay.addWidget(_row("导演视觉", visual_box,
                           "只看切口、冲突、无对白和剧情关键节点；黑屏/断裂可否决"))

        rendered_box = QWidget()
        rendered_lay = QHBoxLayout(rendered_box)
        rendered_lay.setContentsMargins(0, 0, 0, 0)
        rendered_lay.setSpacing(8)
        self.mem_rendered_review = QCheckBox("复审实际成片")
        self.mem_rendered_review.setChecked(
            bool(self._memory_cfg.get("rendered_review_enabled", True)))
        self.mem_rendered_nodes = QSpinBox()
        self.mem_rendered_nodes.setRange(4, 40)
        self.mem_rendered_nodes.setSuffix(" 个成片节点")
        self.mem_rendered_nodes.setValue(int(
            self._memory_cfg.get("rendered_review_max_nodes", 12)))
        rendered_lay.addWidget(self.mem_rendered_review)
        rendered_lay.addWidget(self.mem_rendered_nodes)
        rendered_lay.addStretch(1)
        lay.addWidget(_row("交付复审", rendered_box,
                           "渲染和混音后再次听、看最终 MP4；不通过就阻止交付"))

        vlm_box = QWidget()
        vlm_lay = QHBoxLayout(vlm_box)
        vlm_lay.setContentsMargins(0, 0, 0, 0)
        vlm_lay.setSpacing(8)
        self.mem_visual_vlm = QCheckBox("启用本地视觉模型")
        self.mem_visual_vlm.setChecked(
            bool(self._memory_cfg.get("visual_use_vlm", False)))
        self.mem_vlm_tier = QComboBox()
        self.mem_vlm_tier.addItem("快速（500M）", "fast")
        self.mem_vlm_tier.addItem("准确（2.2B）", "accurate")
        tier_index = self.mem_vlm_tier.findData(
            str(self._memory_cfg.get("visual_vlm_tier") or "fast"))
        self.mem_vlm_tier.setCurrentIndex(max(0, tier_index))
        self.mem_vlm_limit = QSpinBox()
        self.mem_vlm_limit.setRange(0, 12)
        self.mem_vlm_limit.setSuffix(" 个高风险节点")
        self.mem_vlm_limit.setValue(int(
            self._memory_cfg.get("visual_vlm_limit", 2)))
        vlm_lay.addWidget(self.mem_visual_vlm)
        vlm_lay.addWidget(self.mem_vlm_tier)
        vlm_lay.addWidget(self.mem_vlm_limit)
        vlm_lay.addStretch(1)
        lay.addWidget(_row("视觉模型", vlm_box,
                           "规则层始终可用；模型只复核少量高风险画面，默认关闭"))

        save_box = QWidget()
        save_lay = QHBoxLayout(save_box)
        save_lay.setContentsMargins(0, 0, 0, 0)
        save_lay.setSpacing(8)
        self.mem_journal_ms = QSpinBox()
        self.mem_journal_ms.setRange(100, 10000)
        self.mem_journal_ms.setSingleStep(100)
        self.mem_journal_ms.setSuffix(" ms 日志")
        self.mem_journal_ms.setValue(int(self._memory_cfg.get("journal_flush_ms", 1000)))
        self.mem_checkpoint_sec = QSpinBox()
        self.mem_checkpoint_sec.setRange(15, 3600)
        self.mem_checkpoint_sec.setSingleStep(15)
        self.mem_checkpoint_sec.setSuffix(" 秒检查点")
        self.mem_checkpoint_sec.setValue(int(self._memory_cfg.get("checkpoint_seconds", 120)))
        save_lay.addWidget(self.mem_journal_ms)
        save_lay.addWidget(self.mem_checkpoint_sec)
        save_lay.addStretch(1)
        lay.addWidget(_row("自动保存", save_box, "事件批量写入；阶段结束会立即保存"))

        keep_box = QWidget()
        keep_lay = QHBoxLayout(keep_box)
        keep_lay.setContentsMargins(0, 0, 0, 0)
        keep_lay.setSpacing(8)
        self.mem_days = QSpinBox()
        self.mem_days.setRange(1, 3650)
        self.mem_days.setSuffix(" 天")
        self.mem_days.setValue(int(self._memory_cfg.get("session_retention_days", 30)))
        self.mem_limit = QSpinBox()
        self.mem_limit.setRange(1, 1000)
        self.mem_limit.setSuffix(" 个已完成任务")
        self.mem_limit.setValue(int(self._memory_cfg.get("completed_task_limit", 10)))
        keep_lay.addWidget(self.mem_days)
        keep_lay.addWidget(self.mem_limit)
        keep_lay.addStretch(1)
        lay.addWidget(_row("保留策略", keep_box, "清理只删除过期任务，不删除长期经验"))

        actions = QWidget()
        actions_lay = QHBoxLayout(actions)
        actions_lay.setContentsMargins(0, 0, 0, 0)
        actions_lay.setSpacing(8)
        check_btn = QPushButton("检查存储完整性")
        check_btn.setFixedHeight(30)
        check_btn.clicked.connect(self._check_memory_integrity)
        actions_lay.addWidget(check_btn)
        actions_lay.addStretch(1)
        lay.addWidget(actions)
        lay.addStretch(1)
        return page

    def _memory_updates(self):
        return {
            "enabled": bool(self.mem_enabled.isChecked()),
            "root": self.mem_root_edit.text().strip(),
            "session_retention_days": int(self.mem_days.value()),
            "completed_task_limit": int(self.mem_limit.value()),
            "cross_mode_sharing": self.mem_share.currentData() or "facts_only",
            "auto_learn": bool(self.mem_auto_learn.isChecked()),
            "candidate_promotion": self.mem_promotion.currentData() or "validated_only",
            "checkpoint_seconds": int(self.mem_checkpoint_sec.value()),
            "journal_flush_ms": int(self.mem_journal_ms.value()),
            "audio_review_enabled": bool(self.mem_audio_review.isChecked()),
            "director_max_review_windows": int(self.mem_audio_windows.value()),
            "visual_review_enabled": bool(self.mem_visual_review.isChecked()),
            "visual_max_nodes": int(self.mem_visual_nodes.value()),
            "visual_use_vlm": bool(self.mem_visual_vlm.isChecked()),
            "visual_vlm_limit": int(self.mem_vlm_limit.value()),
            "visual_vlm_tier": self.mem_vlm_tier.currentData() or "fast",
            "rendered_review_enabled": bool(self.mem_rendered_review.isChecked()),
            "rendered_review_max_nodes": int(self.mem_rendered_nodes.value()),
        }

    def _pick_memory_root(self):
        start = self.mem_root_edit.text().strip()
        if start and not os.path.isdir(start):
            start = os.path.dirname(start) if os.path.dirname(start) else ""
        folder = QFileDialog.getExistingDirectory(self, "选择导演记忆目录", start)
        if folder:
            self.mem_root_edit.setText(folder)

    def _resolved_memory_root(self):
        import memory_config
        cfg = dict(self._memory_cfg)
        cfg.update(self._memory_updates())
        return memory_config.resolve_root(cfg, home=_home())

    def _open_memory_root(self):
        try:
            root = self._resolved_memory_root()
            os.makedirs(root, exist_ok=True)
            os.startfile(root)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开记忆目录", str(exc))

    def _check_memory_integrity(self):
        try:
            import memory_config
            import task_memory
            cfg = dict(self._memory_cfg)
            cfg.update(self._memory_updates())
            root = memory_config.ensure_root(cfg, home=_home())
            store = task_memory.TaskMemory(root=root)
            try:
                report = store.integrity_check()
            finally:
                store.close()
            missing = len(report.get("missing_artifacts") or [])
            if report.get("ok") and not missing:
                QMessageBox.information(self, "检查完成", "记忆数据库和成果索引完整。")
            else:
                QMessageBox.warning(
                    self, "发现问题",
                    "数据库状态：%s\n外键错误：%d\n缺失成果：%d" % (
                        "正常" if report.get("ok") else "异常",
                        len(report.get("foreign_key_errors") or []), missing))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "检查失败", str(exc))

    def _page_train(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(10)
        edit = QLineEdit(self._cfg.get("train_dir") or "")
        edit.setFixedHeight(28)
        self._bind("train_dir", edit)
        box = QWidget()
        bl = QHBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(edit, 1)
        b = QPushButton("选择…")
        b.setFixedHeight(28)
        b.clicked.connect(lambda: self._pick(edit))
        bl.addWidget(b)
        lay.addWidget(_row("训练目录", box, "放你手剪过的成片与评分"))
        lay.addStretch(1)
        return page

    def _open_subtitle(self):
        try:
            import cards_ui
            cards_ui.SubtitleDialog(self, {}).exec()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", str(exc))

    # ------------------------------------------------------------ 保存
    def accept(self):
        cfg = dict(self._cfg)
        keys = set(cfg.keys()) | set(getattr(self, "_bound_keys", set()))
        for key in keys:
            w = getattr(self, "_w_" + key, None)
            if w is None:
                continue
            if isinstance(w, QCheckBox):
                cfg[key] = "1" if w.isChecked() else "0"
            elif isinstance(w, QLineEdit):
                cfg[key] = w.text().strip()
            elif isinstance(w, QComboBox):
                cfg[key] = w.currentData() or w.currentText()
            elif isinstance(w, QSpinBox):
                cfg[key] = int(w.value())
        import ai_api_settings
        provider = ai_api_settings.normalize_provider(cfg.get("llm_provider"))
        cfg["llm_provider"] = provider
        cfg["llm_api_timeout"] = ai_api_settings.normalize_timeout(
            cfg.get("llm_api_timeout"))
        if ai_api_settings.is_remote(provider):
            base_url = str(cfg.get("llm_api_base_url") or "").strip()
            if not (base_url.startswith("https://") or
                    base_url.startswith("http://")):
                QMessageBox.warning(
                    self, "设置未保存",
                    "AI API 接口地址必须以 http:// 或 https:// 开头。")
                return
            if not str(cfg.get("llm_api_model") or "").strip():
                QMessageBox.warning(self, "设置未保存", "请填写 AI API 模型名。")
                return
        import cut_strength
        keep_percent = cfg.pop("keep_ratio_percent", None)
        if keep_percent is not None:
            cfg["keep_ratio"] = cut_strength.normalize_ratio(keep_percent)
        else:
            cfg["keep_ratio"] = cut_strength.ratio_from_settings(cfg)
        cfg["cut_level"] = cut_strength.level_for_ratio(cfg["keep_ratio"])
        try:
            import memory_config
            import task_memory
            updates = self._memory_updates()
            old_cfg = dict(self._memory_cfg)
            new_cfg = dict(old_cfg)
            new_cfg.update(updates)
            old_root = memory_config.resolve_root(old_cfg, home=_home())
            new_root = memory_config.resolve_root(new_cfg, home=_home())
            if (os.path.normcase(old_root) != os.path.normcase(new_root)
                    and os.path.isfile(os.path.join(old_root, "task_board.sqlite"))):
                answer = QMessageBox.question(
                    self, "记忆目录已变化",
                    "旧目录里已有任务记忆。\n\n"
                    "选择“是”把完整记忆迁移到新目录；选择“否”只切换目录，"
                    "旧记忆仍保留；选择“取消”返回设置。",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                    QMessageBox.Yes)
                if answer == QMessageBox.Cancel:
                    return
                if answer == QMessageBox.Yes:
                    task_memory.migrate_root(old_root, new_root)
            memory_config.ensure_root(new_cfg, home=_home())
            self._memory_cfg = memory_config.save_config(updates, home=_home())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", "导演记忆配置写入失败：%s" % exc)
            return
        try:
            if self._save:
                result = self._save(cfg)
                if result is False:
                    raise OSError("写入 settings.json 失败")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        try:
            import bgm
            bgm.save_conf({
                "folder": self.bgm_folder_edit.text().strip(),
                "level_db": "%.0f" % self.bgm_level.value(),
            }, self._bgm_conf_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", "BGM 配置写入失败：%s" % exc)
            return
        super().accept()


def _home() -> str:
    return os.environ.get("SMARTCUT_HOME") or os.path.dirname(os.path.abspath(__file__))
