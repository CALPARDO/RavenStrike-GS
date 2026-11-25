"""Qt widget styling shared by the console and its dialogs."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap, QPolygonF


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
    app.setPalette(palette)
    app.setStyleSheet("""
        QWidget { color: #192f36; }
        QMainWindow, QScrollArea, QWidget#workspace { background: #f1f5f6; }
        QFrame#header, QFrame#card, QFrame#footer, QTabWidget::pane { background: white; }
        QFrame#header { border-bottom: 1px solid #cedadd; }
        QFrame#footer { border-top: 1px solid #cedadd; }
        QFrame#card, QTabWidget::pane { border: 1px solid #cedadd; border-radius: 7px; }
        QLabel#brand { font-size: 21px; font-weight: 700; color: #143a40; }
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
        QPushButton#abort { color: #9a332c; }
        QPushButton:pressed { background: #daebe9; }
        QPushButton:disabled, QPushButton#primary:disabled, QPushButton#abort:disabled { background: #f3f5f5; color: #738489; border-color: #dce3e4; }
        QLineEdit:focus, QComboBox:focus, QPushButton:focus, QTableWidget:focus { border: 2px solid #13877c; }
        QTabBar::tab { padding: 8px 13px; color: #486169; border-bottom: 3px solid transparent; }
        QTabBar::tab:selected { color: #075e55; border-bottom-color: #0b7167; font-weight: 650; }
        QTableWidget { background: white; alternate-background-color: #f6f9f9; border: 0; }
        QHeaderView::section { background: #f1f5f6; padding: 6px; color: #495f65; border: 0; font-weight: 600; }
    """)
    mark = QPixmap(96, 96)
    mark.fill(Qt.GlobalColor.transparent)
    p = QPainter(mark)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#104c50"))
    p.drawRoundedRect(0, 0, 96, 96, 20, 20)
    for color, points in [
        ("#ffffff", [(13, 26), (52, 40), (69, 27), (83, 32), (71, 38), (48, 70), (42, 51)]),
        ("#75cbb8", [(18, 42), (38, 52), (44, 70), (31, 59)]),
    ]:
        p.setBrush(QColor(color))
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
    p.end()
    app.setWindowIcon(QIcon(mark))
