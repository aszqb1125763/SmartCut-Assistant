# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['F:/自动切片测试/素材卡片工具-一站式版/app_main.py'],
    pathex=[],
    binaries=[],
    datas=[('F:/自动切片测试/素材卡片工具-一站式版/assets', 'assets'), ('F:/自动切片测试/素材卡片工具-一站式版/.venv/Lib/site-packages/faster_whisper/assets', 'faster_whisper/assets')],
    hiddenimports=['PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'faster_whisper', 'ctranslate2', 'av', 'tokenizers', 'huggingface_hub', 'no_console', 'winproc', 'window_guard', 'ui_theme', 'ui_widgets', 'ui_main_v2', 'ui_queue', 'ui_queuepanel', 'ui_settings', 'ui_dialogs', 'ui_thumbs', 'selfcheck', 'seam_continuity', 'paragraph_qc', 'model_editor', 'autoedit_qt', 'ui_autoedit', 'asr', 'asr_hints', 'neutral_text', 'time_sense', 'three_axis', 'utterance', 'ui.llm_bridge', 'pypinyin', 'subtitles', 'bgm'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'pandas', 'scipy', 'torch', 'tkinter', 'PyQt5', 'PyQt6', 'sitecustomize'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='智能剪辑助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='F:/自动切片测试/素材卡片工具-一站式版/build_tools/version_info.txt',
    icon=['F:/自动切片测试/素材卡片工具-一站式版/assets/icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='智能剪辑助手',
)
