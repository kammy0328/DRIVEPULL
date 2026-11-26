BG = "#0d0e11"
PANEL = "#15171c"
PANEL2 = "#1b1e24"
BORDER = "#262a32"
TEXT = "#e8eaef"
MUTED = "#8b919c"
ACCENT = "#5b8cff"
ACCENT_HI = "#7aa2ff"
OK = "#3ecf8e"
WARN = "#f5b94a"
ERR = "#ff6b6b"

def build_qss(check_svg: str) -> str:
    check_svg = check_svg.replace("\\", "/")
    return f"""
* {{
    font-family: "Pretendard", "Segoe UI", "Malgun Gothic", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QDialog, #root {{ background: {BG}; }}
QToolTip {{ background: {PANEL2}; border: 1px solid {BORDER}; padding: 6px; border-radius: 6px; }}

#logo {{ font-size: 17px; font-weight: 800; letter-spacing: 3px; }}
#logoDot {{ color: {ACCENT}; font-size: 17px; font-weight: 800; }}
#muted, QLabel[muted="true"] {{ color: {MUTED}; }}
#sectionTitle {{ font-size: 15px; font-weight: 700; }}
#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; }}

QLineEdit {{
    background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 9px 12px; selection-background-color: {ACCENT};
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
#linkInput {{ font-size: 14px; padding: 12px 14px; }}

QPushButton {{
    background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 8px 14px; font-weight: 600;
}}
QPushButton:hover {{ border-color: #3a404b; background: #20242b; }}
QPushButton:pressed {{ background: #181b20; }}
QPushButton:disabled {{ color: #555a63; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; }}
QPushButton#primary:hover {{ background: {ACCENT_HI}; }}
QPushButton#primary:disabled {{ background: #2c3550; color: #8792ad; }}
QPushButton#ghost {{ background: transparent; border: none; color: {MUTED}; padding: 6px 10px; }}
QPushButton#ghost:hover {{ color: {TEXT}; }}
QPushButton#pill {{ border-radius: 16px; padding: 6px 14px; }}
QPushButton#seg {{
    background: transparent; border: none; border-radius: 8px; padding: 7px 16px; color: {MUTED};
}}
QPushButton#seg:checked {{ background: {PANEL2}; color: {TEXT}; }}
#segWrap {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; }}

QTreeWidget {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px;
    alternate-background-color: #17191e; outline: none; padding: 4px;
}}
QTreeWidget::item {{ height: 30px; border: none; }}
QTreeWidget::item:selected {{ background: #222a3d; color: {TEXT}; }}
QTreeWidget::item:hover {{ background: #1c2028; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 8px 6px; font-size: 12px; font-weight: 600;
}}
QTreeView::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid #3a404b; background: {PANEL2}; }}
QTreeView::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url("{check_svg}"); }}
QTreeView::indicator:indeterminate {{ background: #2c3a5e; border-color: {ACCENT}; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid #3a404b; background: {PANEL2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url("{check_svg}"); }}

QSpinBox {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 8px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: #2e333c; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3b414c; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{ background: #2e333c; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: #262c38; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QMessageBox {{ background: {PANEL}; }}
"""


CHECK_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
             '<path d="M4 8.2l2.6 2.6L12 5.4" fill="none" stroke="white" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round"/></svg>')
