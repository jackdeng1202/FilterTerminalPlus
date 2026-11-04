# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['filterterminal_plus.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# ---- 精简体积：剔除本程序用不到的 Qt 组件 ----
# opengl32sw  软件渲染 OpenGL（~20MB，普通桌面不需要）
# Qt6Quick/Qml/QmlModels/QuickWidgets/Pdf  QML 全家桶（本程序只用 Widgets）
# translations  Qt 自带界面翻译（UI 全是自绘中文，缺了只回退英文）
# plugins/networkinformation  网络状态插件（无网络功能）
# plugins/generic  触摸板等通用输入插件（车载/桌面键盘鼠标不需要）
_DROP = ('opengl32sw', 'Qt6Quick', 'Qt6Qml', 'Qt6Pdf', 'translations',
         'networkinformation', 'generic')
a.binaries = [b for b in a.binaries if not any(p in b[0] for p in _DROP)]
a.datas = [d for d in a.datas if not any(p in d[0] for p in _DROP)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FilterTerminalPlus',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='FilterTerminalPlus',
)
