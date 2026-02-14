# -*- mode: python ; coding: utf-8 -*-
# spec-review.spec
# PyInstaller spec file for MEP Spec Review GUI executable
# 
# Build command: pyinstaller spec-review.spec --clean
# Output: dist/MEP-Spec-Review.exe (single file)

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Collect data files for dependencies that bundle assets
# CustomTkinter: themes, fonts, images
# tiktoken: tokenizer data files (cl100k_base.tiktoken, etc.)
datas = [
    ('src', 'src'),  # Our source package
]
datas += collect_data_files('customtkinter')
datas += collect_data_files('tiktoken')

# Hidden imports that PyInstaller might miss
hiddenimports = [
    # tiktoken internals
    'tiktoken_ext.openai_public',
    'tiktoken_ext',
    # anthropic SDK
    'anthropic',
    'anthropic._streaming',
    'anthropic._base_client',
    'anthropic.resources',
    # httpx (used by anthropic)
    'httpx',
    'httpcore',
    'h11',
    'anyio',
    'sniffio',
    # python-docx internals
    'docx',
    'docx.oxml.ns',
    'docx.oxml',
    'lxml',
    'lxml.etree',
    'lxml._elementpath',
    # Our src modules (explicit)
    'src',
    'src.pipeline',
    'src.extractor',
    'src.preprocessor',
    'src.prompts',
    'src.report',
    'src.reviewer',
    'src.tokenizer',
]

# Collect all submodules for complex packages
hiddenimports += collect_submodules('anthropic')
hiddenimports += collect_submodules('tiktoken')

a = Analysis(
    ['gui.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unnecessary modules to reduce size
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'pytest',
        'setuptools',
        'wheel',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MEP-Spec-Review',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window - GUI only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
