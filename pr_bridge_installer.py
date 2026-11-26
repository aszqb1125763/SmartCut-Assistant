# -*- coding: utf-8 -*-
"""Premiere CEP installer: discover, stage, verify, back up, then publish.

The filesystem helpers deliberately do not import Qt, so installation and
rollback can be verified without opening a desktop window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import sys
import uuid
import xml.etree.ElementTree as ET


EXTENSION_ID = "com.smartcut.prbridge"
DEBUG_VERSIONS = tuple(range(8, 13))


@dataclass(frozen=True)
class ExtensionTarget:
    root: Path
    label: str
    system: bool = False


@dataclass(frozen=True)
class PremiereInstall:
    name: str
    path: Path


@dataclass(frozen=True)
class InstallResult:
    target: Path
    backup: Path | None
    version: str
    file_count: int
    warnings: tuple[str, ...] = ()


def application_home(home=None) -> Path:
    return Path(home or os.environ.get("SMARTCUT_HOME") or
                Path(__file__).resolve().parent).resolve()


def extension_source(home=None) -> Path:
    return application_home(home) / "pr_bridge"


def discover_targets(environ=None) -> list[ExtensionTarget]:
    env = os.environ if environ is None else environ
    roaming = Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    common = Path(env.get("CommonProgramFiles") or
                  r"C:\Program Files\Common Files")
    common_x86 = Path(env.get("CommonProgramFiles(x86)") or
                      r"C:\Program Files (x86)\Common Files")
    candidates = [
        ExtensionTarget(roaming / "Adobe" / "CEP" / "extensions",
                        "当前用户（推荐，无需管理员）"),
        ExtensionTarget(common / "Adobe" / "CEP" / "extensions",
                        "系统公共目录（可能需要管理员）", True),
        ExtensionTarget(common_x86 / "Adobe" / "CEP" / "extensions",
                        "系统 x86 公共目录（可能需要管理员）", True),
    ]
    result, seen = [], set()
    for row in candidates:
        key = os.path.normcase(os.path.abspath(row.root))
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def registered_premiere() -> list[PremiereInstall]:
    """Adobe allows custom install locations, so folders alone are insufficient."""
    if os.name != "nt":
        return []
    import winreg
    result = []

    def value(key, name):
        try:
            return str(winreg.QueryValueEx(key, name)[0] or "")
        except OSError:
            return ""

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        app_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Adobe Premiere Pro.exe"
        try:
            with winreg.OpenKey(hive, app_path) as key:
                executable = Path(value(key, "").strip('"'))
                if executable.is_file():
                    folder = executable.parent
                    result.append(PremiereInstall(folder.name, folder))
        except OSError:
            pass
        for base in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                with winreg.OpenKey(hive, base) as listing:
                    index = 0
                    while True:
                        try:
                            child = winreg.EnumKey(listing, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(listing, child) as key:
                                name = value(key, "DisplayName")
                                location = value(key, "InstallLocation")
                        except OSError:
                            continue
                        if "adobe premiere pro" not in name.lower() or not location:
                            continue
                        folder = Path(location.strip('"'))
                        if (folder / "Adobe Premiere Pro.exe").is_file():
                            result.append(PremiereInstall(name, folder))
                        else:
                            for subfolder in folder.glob("Adobe Premiere Pro*"):
                                if (subfolder / "Adobe Premiere Pro.exe").is_file():
                                    result.append(PremiereInstall(subfolder.name, subfolder))
            except OSError:
                continue
    return result


def discover_premiere(environ=None, program_roots=None, registered=None) -> list[PremiereInstall]:
    env = os.environ if environ is None else environ
    roots = program_roots if program_roots is not None else [
        env.get("ProgramW6432"), env.get("ProgramFiles", r"C:\Program Files"),
        env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    result, seen = [], set()
    registered = (registered_premiere() if program_roots is None else []) if registered is None else registered
    for row in registered:
        key = os.path.normcase(os.path.abspath(row.path))
        if key not in seen:
            seen.add(key)
            result.append(row)
    for root in roots:
        if not root:
            continue
        adobe = Path(root) / "Adobe"
        try:
            candidates = sorted(adobe.glob("Adobe Premiere Pro*"))
        except OSError:
            continue
        for folder in candidates:
            if not folder.is_dir():
                continue
            key = os.path.normcase(os.path.abspath(folder))
            if key in seen:
                continue
            seen.add(key)
            result.append(PremiereInstall(folder.name, folder))
    return result


def manifest_version(extension) -> str:
    manifest = Path(extension) / "CSXS" / "manifest.xml"
    return ET.parse(manifest).getroot().get("ExtensionBundleVersion", "未知")


def tree_hashes(folder) -> dict[str, str]:
    folder = Path(folder)
    if folder.is_symlink():
        raise ValueError("插件目录不能是符号链接：%s" % folder)
    result = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError("插件包包含符号链接：%s" % path)
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        result[path.relative_to(folder).as_posix()] = digest.hexdigest()
    return result


def inspect_target(target_root, source=None) -> dict:
    target = Path(target_root) / EXTENSION_ID
    row = {"path": target, "installed": target.exists(), "version": "",
           "matches": False, "error": ""}
    if not target.exists():
        return row
    try:
        row["version"] = manifest_version(target)
        if source is not None:
            row["matches"] = tree_hashes(source) == tree_hashes(target)
    except (OSError, ValueError, ET.ParseError) as exc:
        row["error"] = str(exc)
    return row


def enable_debug_mode() -> list[str]:
    """Only current-user registry keys; no elevation and no Premiere processes."""
    if os.name != "nt":
        return ["当前系统不是 Windows，未设置 CEP 调试开关。"]
    import winreg
    warnings = []
    for version in DEBUG_VERSIONS:
        try:
            key_path = r"Software\Adobe\CSXS.%d" % version
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                    winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "PlayerDebugMode", 0, winreg.REG_SZ, "1")
        except OSError as exc:
            warnings.append("CSXS.%d 调试开关设置失败：%s" % (version, exc))
    return warnings


def install_extension(source, target_root, *, configure_debug=True,
                      registry_writer=None) -> InstallResult:
    if not str(target_root or "").strip():
        raise ValueError("请选择 CEP extensions 文件夹")
    source_input = Path(source).expanduser()
    if source_input.is_symlink():
        raise ValueError("插件源目录不能是符号链接")
    source_path = source_input.resolve()
    if not source_path.is_dir():
        raise FileNotFoundError("找不到插件源目录：%s" % source_path)
    for entry in ("CSXS/manifest.xml", "index.html", "js/main.js",
                  "js/CSInterface.js", "jsx/host.jsx"):
        if not (source_path / entry).is_file():
            raise FileNotFoundError("插件包不完整：缺少 %s" % entry)
    version = manifest_version(source_path)
    expected = tree_hashes(source_path)
    root = Path(os.path.expandvars(str(target_root))).expanduser().resolve()
    if root == Path(root.anchor):
        raise ValueError("请选择 CEP extensions 文件夹，不能安装到磁盘根目录")
    if source_path == root or source_path in root.parents:
        raise ValueError("安装目标不能位于插件源目录内")
    target = root / EXTENSION_ID
    if target == source_path:
        raise ValueError("安装目标不能覆盖插件源目录")
    if target.is_symlink():
        raise ValueError("现有插件是符号链接，请选择普通 CEP extensions 文件夹")
    if target.exists() and not target.is_dir():
        raise ValueError("安装目标已存在同名文件：%s" % target)

    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".smartcut-install.lock"
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("此目录有另一项插件安装正在运行：%s" % lock) from exc
    os.close(lock_fd)
    stage = root / (".smartcut-stage-" + uuid.uuid4().hex)
    backup = None
    published = False
    restored = True
    try:
        shutil.copytree(source_path, stage)
        if tree_hashes(stage) != expected:
            raise RuntimeError("复制后的插件哈希校验失败，原插件未修改")
        if target.exists():
            # Keep backups outside extensions so CEP will not load a second
            # copy with the same extension identifier.
            backup_root = root.parent / "SmartCut-CEP备份" / EXTENSION_ID
            backup_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup = backup_root / (stamp + "_" + uuid.uuid4().hex[:8])
            os.replace(target, backup)
        try:
            os.replace(stage, target)
            published = True
            if tree_hashes(target) != expected:
                raise RuntimeError("安装后的插件哈希校验失败")
        except Exception:
            # A failed publish must leave the previous installation usable.
            # Move our new directory out before restoring; never recursively
            # remove a user-selected directory.
            try:
                if published:
                    os.replace(target, stage)
                if backup is not None:
                    os.replace(backup, target)
            except OSError as rollback_error:
                restored = False
                raise RuntimeError("安装失败且无法自动恢复，请从备份恢复：%s；%s" %
                                   (backup, rollback_error)) from rollback_error
            raise
    finally:
        if stage.exists() and restored:
            shutil.rmtree(stage)
        lock.unlink(missing_ok=True)

    warnings = []
    if configure_debug:
        try:
            warnings.extend((registry_writer or enable_debug_mode)() or [])
        except OSError as exc:
            warnings.append("插件文件已安装，但 CEP 调试开关设置失败：%s" % exc)
    return InstallResult(target, backup, version, len(expected), tuple(warnings))


def create_installer_dialog(parent=None, home=None):
    from product_info import credits_html
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
        QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    )

    class InstallerDialog(QDialog):
        def __init__(self):
            super().__init__(parent)
            self.home = application_home(home)
            self.source = extension_source(self.home)
            self.targets = discover_targets()
            self.setWindowTitle("安装 / 更新 Premiere 工程插件 · 智能剪辑助手")
            self.resize(840, 720)
            self.setMinimumSize(760, 650)
            self.setStyleSheet(
                "QDialog{background:#202124;color:#ddd;}"
                "QLabel{color:#ccc;}"
                "QPushButton{padding:6px 14px;border:1px solid #555;"
                "border-radius:4px;background:#303338;color:#ddd;}"
                "QPushButton:hover{background:#3a3f45;}"
                "QPushButton#install{background:#327451;border-color:#438d65;}"
                "QComboBox,QPlainTextEdit,QTreeWidget{background:#191b1e;"
                "color:#ddd;border:1px solid #454950;border-radius:4px;}"
                "QComboBox{padding:6px;}QHeaderView::section{background:#2b2e33;"
                "color:#ccc;border:0;padding:5px;}"
            )
            layout = QVBoxLayout(self)
            layout.setContentsMargins(20, 18, 20, 16)
            layout.setSpacing(11)
            title = QLabel("Premiere 工程插件")
            title.setStyleSheet("font-size:20px;font-weight:600;color:#eee;")
            layout.addWidget(title)
            hint = QLabel("支持 PR 2020 及之后仍提供 CEP 的版本。字幕轨能力自动检测；"
                          "旧版会导入简繁 SRT，工程仍可继续编辑。")
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.source_label = QLabel()
            self.source_label.setWordWrap(True)
            layout.addWidget(self.source_label)
            self.premiere_label = QLabel()
            self.premiere_label.setWordWrap(True)
            layout.addWidget(self.premiere_label)
            self.table = QTreeWidget()
            self.table.setHeaderLabels(["CEP 目录", "安装位置", "状态"])
            self.table.setColumnWidth(0, 165)
            self.table.setColumnWidth(1, 390)
            self.table.setRootIsDecorated(False)
            self.table.setMinimumHeight(130)
            self.table.itemClicked.connect(self.choose_detected)
            layout.addWidget(self.table, 1)
            layout.addWidget(QLabel("安装到 CEP extensions 文件夹"))
            target_row = QHBoxLayout()
            self.target_combo = QComboBox()
            self.target_combo.setEditable(True)
            self.target_combo.setMinimumContentsLength(36)
            self.target_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            target_row.addWidget(self.target_combo, 1)
            browse = QPushButton("浏览…")
            browse.clicked.connect(self.browse)
            target_row.addWidget(browse)
            layout.addLayout(target_row)
            self.target_label = QLabel()
            self.target_label.setWordWrap(True)
            self.target_label.setStyleSheet("color:#96abc0;font-size:11px;")
            layout.addWidget(self.target_label)
            self.target_combo.currentTextChanged.connect(self.refresh_target)
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumHeight(115)
            self.log.setPlaceholderText("安装会先复制并校验，再备份旧版和切换；不会关闭 Premiere。")
            layout.addWidget(self.log)
            restart = QLabel("已打开的 Premiere 需要手动重启，再从“窗口 → 扩展（旧版）”打开“智能剪辑桥”。")
            restart.setWordWrap(True)
            restart.setStyleSheet("color:#999;font-size:11px;")
            layout.addWidget(restart)
            buttons = QHBoxLayout()
            refresh = QPushButton("重新检测")
            refresh.clicked.connect(self.refresh_scan)
            buttons.addWidget(refresh)
            buttons.addStretch(1)
            self.install_button = QPushButton("安装 / 更新插件")
            self.install_button.setObjectName("install")
            self.install_button.clicked.connect(self.install)
            buttons.addWidget(self.install_button)
            close = QPushButton("关闭")
            close.clicked.connect(self.accept)
            buttons.addWidget(close)
            layout.addLayout(buttons)
            credits = QLabel(credits_html() + " · <a href='notice'>完整署名与贡献</a>")
            credits.setStyleSheet("font-size:10px;color:#888;")
            credits.setWordWrap(True)
            credits.linkActivated.connect(self.open_notice)
            layout.addWidget(credits)
            self.refresh_scan()

        def refresh_scan(self):
            current = self.target_combo.currentText().strip()
            self.targets = discover_targets()
            self.target_combo.blockSignals(True)
            self.target_combo.clear()
            for target in self.targets:
                self.target_combo.addItem(str(target.root))
            self.target_combo.setCurrentText(current or str(self.targets[0].root))
            self.target_combo.blockSignals(False)
            try:
                version = manifest_version(self.source)
                self.source_label.setText("随软件提供的插件：v%s · %s" % (version, self.source))
                self.install_button.setEnabled(True)
            except (OSError, ValueError, ET.ParseError) as exc:
                self.source_label.setText("插件源文件不可用：%s" % exc)
                self.install_button.setEnabled(False)
            premieres = discover_premiere()
            names = "、".join(row.name for row in premieres)
            self.premiere_label.setText("检测到的 Premiere：%s" %
                                        (names or "常见目录中未发现，可继续安装到用户 CEP 目录"))
            self.premiere_label.setToolTip("\n".join(str(row.path) for row in premieres))
            self.table.clear()
            for target in self.targets:
                info = inspect_target(target.root, self.source)
                if info["error"]:
                    state = "需修复 / 权限受限"
                elif info["matches"]:
                    state = "v%s · 已是当前版" % info["version"]
                elif info["installed"]:
                    state = "v%s · 可更新" % info["version"]
                else:
                    state = "未安装"
                item = QTreeWidgetItem([target.label, str(target.root), state])
                item.setData(0, Qt.UserRole, str(target.root))
                item.setToolTip(1, str(target.root))
                item.setToolTip(2, info["error"] or state)
                self.table.addTopLevelItem(item)
            self.refresh_target()

        def choose_detected(self, item, _column):
            self.target_combo.setCurrentText(str(item.data(0, Qt.UserRole)))

        def refresh_target(self, *_):
            value = self.target_combo.currentText().strip()
            if not value:
                self.target_label.setText("请选择安装目录")
                return
            system = any(os.path.normcase(os.path.abspath(row.root)) ==
                         os.path.normcase(os.path.abspath(value)) and row.system
                         for row in self.targets)
            hint = "系统目录可能需要管理员权限；推荐选择当前用户目录。" if system else "当前用户目录通常无需管理员权限。"
            self.target_label.setText("插件位置：%s\n%s" %
                                      (Path(value) / EXTENSION_ID, hint))

        def browse(self):
            folder = QFileDialog.getExistingDirectory(
                self, "选择 CEP extensions 文件夹", self.target_combo.currentText())
            if folder:
                chosen = Path(folder)
                if chosen.name.casefold() == EXTENSION_ID.casefold():
                    chosen = chosen.parent
                self.target_combo.setCurrentText(str(chosen))

        def install(self):
            value = self.target_combo.currentText().strip()
            if not value:
                QMessageBox.information(self, "选择目录", "先选择 CEP extensions 文件夹。")
                return
            self.install_button.setEnabled(False)
            self.log.appendPlainText("正在复制、校验并安装…")
            try:
                result = install_extension(self.source, value)
                self.log.appendPlainText("已安装 v%s，%d 个文件校验通过。\n位置：%s" %
                                         (result.version, result.file_count, result.target))
                if result.backup:
                    self.log.appendPlainText("旧版备份：%s" % result.backup)
                if result.warnings:
                    self.log.appendPlainText("\n".join(result.warnings))
                else:
                    self.log.appendPlainText("已设置当前用户 CSXS.8–12 调试开关。")
                self.log.appendPlainText("安装完成。请手动重启 Premiere 后打开智能剪辑桥。")
                self.refresh_scan()
            except PermissionError as exc:
                self.log.appendPlainText("权限不足，旧插件已保留或恢复：%s\n"
                                         "请改选“当前用户”CEP 目录，或以管理员权限运行安装器。" % exc)
            except Exception as exc:  # noqa: BLE001
                self.log.appendPlainText("安装未完成：%s" % exc)
            finally:
                self.install_button.setEnabled(True)

        def open_notice(self, _link):
            if _link != "notice":
                QDesktopServices.openUrl(QUrl(_link))
                return
            notice = self.home / "NOTICE.md"
            if notice.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(notice)))
            else:
                QMessageBox.information(self, "署名与贡献", "初版创意：瓜子神教大主教\n"
                                        "全功能与剪辑模型结构：不无聊的子枫哥\n"
                                        "AI 辅助开发：DeepSeek、OpenAI Codex")

    return InstallerDialog()


def show_installer(parent=None, home=None) -> int:
    return create_installer_dialog(parent, home).exec()


def main() -> int:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    _ = app
    show_installer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
