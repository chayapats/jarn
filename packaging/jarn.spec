# PyInstaller spec for J.A.R.N. — produces a single-file `jarn` binary.
#
# Build:  uv run pyinstaller packaging/jarn.spec --noconfirm
# Output: dist/jarn  (a standalone executable; no Python required to run)
#
# Textual, deepagents, and the langchain/langgraph stack ship data files and use
# dynamic imports, so we collect them wholesale to avoid missing-module errors.

from PyInstaller.utils.hooks import collect_all

_PACKAGES = [
    "jarn",
    "textual",
    "rich",
    "prompt_toolkit",
    "deepagents",
    "langchain",
    "langchain_core",
    "langgraph",
    "langchain_openai",
    "langchain_anthropic",
    "langchain_ollama",
    "langchain_google_genai",
    "langchain_mistralai",
    "langchain_mcp_adapters",
    "ruamel.yaml",
    "tiktoken",
    "keyring",
]

datas, binaries, hiddenimports = [], [], []
for pkg in _PACKAGES:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="jarn",
    console=True,        # TUI app needs a console
    onefile=True,
    strip=False,
    upx=True,
)
