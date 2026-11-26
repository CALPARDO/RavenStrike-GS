"""Qt widget styling shared by the console and its dialogs."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPalette

ASSETS = Path(__file__).resolve().parent / "assets"


def icon(name):
    return QIcon(str(ASSETS / "icons" / f"{name}.svg"))


def field_theme(app):
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    app.setStyle("Fusion")
    palette = app.style().standardPalette()
    for role, color in {
        "Window": "#f1f5f6",
        "WindowText": "#192f36",
        "Text": "#192f36",
        "Base": "#ffffff",
        "Button": "#ffffff",
        "ButtonText": "#192f36",
        "Highlight": "#0b6b64",
        "HighlightedText": "#ffffff",
        "PlaceholderText": "#586d74",
        "ToolTipBase": "#ffffff",
        "ToolTipText": "#192f36",
    }.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    for role in ("Text", "WindowText", "ButtonText"):
        palette.setColor(
            QPalette.ColorGroup.Disabled, getattr(QPalette.ColorRole, role), QColor("#858e93")
        )
    app.setPalette(palette)
    app.setStyleSheet("""
        QWidget { color: #192f36; }
        QMainWindow, QScrollArea, QWidget#workspace { background: #f1f5f6; }
        QFrame#header, QFrame#card, QFrame#footer, QTabWidget::pane { background: white; }
        QFrame#header { background: #172f38; border-bottom: 2px solid #288c84; }
        QLabel#headerText { color: #d4e8e7; font-weight: 550; }
        QLabel#meter, QLabel#clock { font-family: monospace; font-weight: 650; }
        QLabel#phase { color: #09685f; font-size: 15px; font-weight: 650; padding-bottom: 4px; }
        QLabel#reviewHeading { color: #193640; font-size: 24px; font-weight: 700; }
        QLabel#reviewSubtitle { color: #445c65; font-size: 15px; }
        QFrame#scrim { background: rgba(21, 40, 49, 76); }
        QDialog { background: #fcfdfd; }
        QLabel#reviewNotice { background: #f8f3e7; color: #675022; padding: 15px; border-radius: 6px; }
        QPushButton#reviewAction { background: #fff3d7; color: #70490c; border: 1px solid #d0ac64; padding: 9px; }
        QPushButton#reviewAction:hover { background: #f8e6bb; }
        QWidget[pending="true"] { background-color: #fff3da; }
        QSplitter::handle { background-color: #d3dfe0; }
        QMenu { background: white; }
        QMenu::item:disabled, QMenu::item:disabled:selected, QMenuBar::item:disabled { color: #858e93; background: transparent; }
        QMenu::item:enabled:selected { background-color: #d8eeea; color: #134e47; }
        QFrame#footer { border-top: 1px solid #cedadd; }
        QFrame#card, QTabWidget::pane { border: 1px solid #cedadd; border-radius: 7px; }
        QLabel#brand { font-size: 21px; font-weight: 700; color: #f3fbfb; }
        QLabel#planKey, QLabel#trackKey { font-weight: 650; padding: 2px 6px; }
        QLabel#planKey { color: #344fa9; } QLabel#trackKey { color: #006858; }
        QLabel#obstacleKey { color: #895510; font-weight: 650; padding: 2px 6px; }
        QWidget#layerSwitch { background: #eaf0f1; border: 1px solid #d5dfe2; border-radius: 6px; }
        QPushButton#layerButton { background: transparent; color: #49616a; border: none; padding: 4px 10px; border-radius: 4px; }
        QPushButton#layerButton:checked { background: #264d56; color: white; }
        QPushButton#layerButton:focus { border: 1px solid #549e97; }
        QPushButton#mapTool { background: transparent; border: 1px solid transparent; color: #355565; padding: 4px 7px; }
        QPushButton#mapTool:hover { background: #e4edee; color: #173f49; }
        QPushButton#mapTool:disabled { color: #869699; }
        QPushButton#targetAction { border-color: #2f7871; color: #12685f; background: #edf7f4; }
        QPushButton#targetAction:disabled { color: #778789; background: #f2f4f4; border-color: #d6dfe0; }
        QLabel#section { color: #405961; font-weight: 650; padding-top: 5px; }
        QLabel#closed { background: #e4eceb; color: #445f5d; border-radius: 9px; padding: 3px 9px; }
        QToolButton#healthToggle { background: white; color: #23434a; border: 1px solid #adbdc0; border-radius: 5px; padding: 4px 8px; }
        QToolButton#healthToggle:checked { background: #d8eeea; border-color: #41847d; }
        QFrame#telemetry { background: #fff; border: 1px solid #d3dfe0; border-radius: 5px; }
        QPlainTextEdit#timeline { background: #f7f9fa; border: 0; border-left: 2px solid #b6cbce; border-radius: 0; padding-left: 7px; }
        QFrame#rule { color: #c6d2d4; max-height: 16px; }
        QLabel#heading { font-size: 17px; font-weight: 650; }
        QLabel#muted, QLabel#field { color: #4f666d; }
        QLabel#field { font-weight: 600; }
        QLabel#link { border-radius: 9px; padding: 4px 10px; font-weight: 600; }
        QLabel#evidence { background: #f1f5f6; border: 1px solid #dce5e6; border-radius: 4px; color: #50686e; }
        QLineEdit, QComboBox, QPlainTextEdit { background: white; border: 1px solid #adbdc0; border-radius: 4px; padding: 5px; }
        QPushButton { background: white; color: #23434a; border: 1px solid #adbdc0; border-radius: 5px; padding: 6px 10px; font-weight: 600; }
        QPushButton:hover { background: #edf5f4; }
        QPushButton#primary { background: #09685f; color: white; border-color: #09685f; }
        QPushButton#primary:hover { background: #07564e; }
        QPushButton#abort { color: #9a332c; background: #fff6f3; border-color: #d6a198; }
        QPushButton:pressed { background: #daebe9; }
        QPushButton:disabled, QPushButton#primary:disabled, QPushButton#abort:disabled { background: #f3f5f5; color: #738489; border-color: #dce3e4; }
        QLineEdit:focus, QComboBox:focus, QPushButton:focus, QTableWidget:focus { border: 2px solid #13877c; }
        QTabBar::tab { padding: 8px 13px; color: #486169; border-bottom: 3px solid transparent; }
        QTabBar::tab:selected { color: #075e55; border-bottom-color: #0b7167; font-weight: 650; }
        QTableWidget { background: white; alternate-background-color: #f6f9f9; border: 0; }
        QHeaderView::section { background: #f1f5f6; padding: 6px; color: #495f65; border: 0; font-weight: 600; }
    """)
    app.setWindowIcon(QIcon(str(ASSETS / "ravenstrike.svg")))
