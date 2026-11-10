"""Shared penguin icon for both PySide6 windows, including one-file EXEs."""
from pathlib import Path
import sys
from PySide6.QtGui import QIcon


def load_app_icon() -> QIcon:
    # PyInstaller onefile unpacks --add-data into sys._MEIPASS at runtime.
    resource_dir = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return QIcon(str(resource_dir / 'assets' / 'penguin_icon.png'))
