# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置
#
# 关键点(踩过的坑):
#  1. ultralytics 的子模块必须显式收集, 否则打包后 import 失败
#  2. torchvision 的原生扩展(torchvision._C / torchvision.image)必须收进去,
#     否则 exe 里一推理就报 "operator torchvision::nms does not exist",
#     表现就是"有人经过也不截图"
#  3. torch / torchvision 的 DLL 要一并收集
from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

hiddenimports = []
hiddenimports += collect_submodules('ultralytics')

# torchvision 原生扩展: 少了这些, torchvision::nms 算子不存在, exe 里推理直接失败
# 注意 torchvision 0.29 用的是 _C_stable.pyd / image_stable.pyd, 不是 _C.pyd
hiddenimports += ['torchvision._C', 'torchvision.image', 'torchvision.ops', 'torchvision.extension']

binaries = []
# 关键: PyInstaller 的 collect_dynamic_libs 默认只找 *.dll, 不收 *.pyd,
#       而 _C_stable.pyd 又是用 torch.ops.load_library(路径) 动态加载的,
#       必须手动把 *.pyd 也一起收进来, 否则打包后报
#       "operator torchvision::nms does not exist"
binaries += collect_dynamic_libs('torchvision', search_patterns=['*.pyd', '*.dll'])
binaries += collect_dynamic_libs('torch')

a = Analysis(
    ['main_opencv.py'],
    pathex=[],
    binaries=binaries,
    datas=[('yolov8n.pt', '.')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ScreenPersonMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app.ico',
    version='version_info.txt',
)
