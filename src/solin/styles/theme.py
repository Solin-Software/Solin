COLORS = {
    "bg0": "#0d1117",
    "bg1": "#161b22",
    "bg2": "#21262d",
    "bg3": "#2d333b",
    "accent": "#4f8cc9",
    "accent2": "#388bfd",
    "accent_muted": "#1f3a5f",
    "text_primary": "#e6edf3",
    "text_secondary": "#8b949e",
    "text_muted": "#484f58",
    "border": "#30363d",
    "border_muted": "#21262d",
    "success": "#3fb950",
    "warning": "#d29922",
    "danger": "#f85149",
    "projection": "#2ea043",
    "sidebar_w": "64px",
    "sidebar_expanded_w": "220px",
}

SCROLLBAR_STYLESHEET = """
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical {
    background: %(bg1)s;
    width: 6px;
    border-radius: 3px;
}
QScrollBar::handle:vertical {
    background: %(bg3)s;
    border-radius: 3px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: %(text_secondary)s;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal {
    background: %(bg1)s;
    height: 6px;
    border-radius: 3px;
}
QScrollBar::handle:horizontal {
    background: %(bg3)s;
    border-radius: 3px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover { background: %(text_secondary)s; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
""" % COLORS

STYLESHEET = """
/* === Global === */
QWidget {
    background-color: %(bg0)s;
    color: %(text_primary)s;
    font-family: "Segoe UI", "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
    border: none;
    outline: none;
}

QMainWindow, QDialog {
    background-color: %(bg0)s;
}

/* === Scrollbars === */
QScrollBar:vertical {
    background: %(bg1)s;
    width: 6px;
    border-radius: 3px;
}
QScrollBar::handle:vertical {
    background: %(bg3)s;
    border-radius: 3px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: %(text_secondary)s;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }

QScrollBar:horizontal {
    background: %(bg1)s;
    height: 6px;
    border-radius: 3px;
}
QScrollBar::handle:horizontal {
    background: %(bg3)s;
    border-radius: 3px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover { background: %(text_secondary)s; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* === Sidebar === */
#Sidebar {
    background-color: %(bg1)s;
    border-right: 1px solid %(border)s;
}

#SidebarBtn {
    background: transparent;
    border: none;
    border-radius: 10px;
    padding: 10px;
    color: %(text_secondary)s;
    font-size: 12px;
    text-align: left;
}
#SidebarBtn:hover {
    background-color: %(bg2)s;
    color: %(text_primary)s;
}
#SidebarBtn[active="true"] {
    background-color: %(accent_muted)s;
    color: %(accent2)s;
}

#SidebarLangBtn {
    background: %(bg2)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    padding: 6px 10px;
    color: %(text_secondary)s;
    font-size: 11px;
    font-weight: 600;
}
#SidebarLangBtn:hover {
    background: %(bg3)s;
    color: %(text_primary)s;
}

/* === Content Area === */
#ContentArea {
    background-color: %(bg0)s;
}

/* === Cards === */
#Card {
    background-color: %(bg1)s;
    border: 1px solid %(border)s;
    border-radius: 12px;
}

/* === Section Headers === */
#SectionTitle {
    background: transparent;
    font-size: 20px;
    font-weight: 700;
    color: %(text_primary)s;
}
#SectionSubtitle {
    background: transparent;
    font-size: 12px;
    color: %(text_secondary)s;
}

/* === Search Bar === */
#SearchBar {
    background-color: %(bg2)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    padding: 8px 14px;
    color: %(text_primary)s;
    font-size: 13px;
    selection-background-color: %(accent_muted)s;
}
#SearchBar:focus {
    border-color: %(accent)s;
}
#SearchBar::placeholder {
    color: %(text_muted)s;
}

/* === Song List === */
#SongList {
    background-color: transparent;
    border: none;
    outline: none;
}
#SongList::item {
    background: %(bg1)s;
    border: 1px solid %(border_muted)s;
    border-radius: 8px;
    padding: 0px;
    margin: 2px 0;
    color: %(text_primary)s;
}
#SongList::item:hover {
    background: %(bg2)s;
    border-color: %(border)s;
}
#SongList::item:selected {
    background: %(accent_muted)s;
    border-color: %(accent)s;
    color: %(text_primary)s;
}

/* === Buttons === */
QPushButton {
    background-color: %(bg2)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    padding: 8px 18px;
    color: %(text_primary)s;
    font-weight: 500;
}
QPushButton:hover {
    background-color: %(bg3)s;
    border-color: %(text_secondary)s;
}
QPushButton:pressed {
    background-color: %(bg1)s;
}
QPushButton:disabled {
    color: %(text_muted)s;
    border-color: %(border_muted)s;
}

#PrimaryBtn {
    background-color: %(accent)s;
    border: none;
    border-radius: 8px;
    padding: 9px 20px;
    color: white;
    font-weight: 600;
}
#PrimaryBtn:hover {
    background-color: %(accent2)s;
}
#PrimaryBtn:disabled {
    background-color: %(accent_muted)s;
    color: %(text_secondary)s;
}

#DangerBtn {
    background-color: transparent;
    border: 1px solid %(danger)s;
    color: %(danger)s;
    border-radius: 8px;
    padding: 8px 18px;
    font-weight: 500;
}
#DangerBtn:hover {
    background-color: %(danger)s;
    color: white;
}

#SuccessBtn {
    background-color: %(projection)s;
    border: none;
    border-radius: 8px;
    padding: 9px 20px;
    color: white;
    font-weight: 600;
}
#SuccessBtn:hover {
    background-color: #46b556;
}

/* === Player Controls === */
#PlayerCard {
    background-color: %(bg1)s;
    border: 1px solid %(border)s;
    border-radius: 14px;
    padding: 16px;
}

#PlayerTitle {
    background: transparent;
    font-size: 15px;
    font-weight: 600;
    color: %(text_primary)s;
}
#PlayerSubtitle {
    background: transparent;
    font-size: 12px;
    color: %(text_secondary)s;
}

QSlider::groove:horizontal {
    height: 4px;
    background: %(bg3)s;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: %(accent2)s;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}
QSlider::handle:horizontal:hover {
    background: white;
    width: 16px;
    height: 16px;
    margin: -6px 0;
    border-radius: 8px;
}
QSlider::sub-page:horizontal {
    background: %(accent)s;
    border-radius: 2px;
}

/* === Status Bar === */
#StatusBar {
    background-color: %(bg1)s;
    border-top: 1px solid %(border)s;
    /* padding: 4px 16px; */
}
#StatusLabel {
    color: %(text_secondary)s;
    font-size: 11px;
}
#StatusActive {
    color: %(success)s;
    font-size: 11px;
    font-weight: 600;
}

/* === Settings === */
#SettingsGroup {
    background: %(bg1)s;
    border: 1px solid %(border)s;
    border-radius: 12px;
    padding: 16px;
}
#SettingsGroupTitle {
    background: transparent;
    font-size: 11px;
    font-weight: 700;
    color: %(text_secondary)s;
    text-transform: uppercase;
    letter-spacing: 0.8px;
}

#LangCard {
    background: %(bg2)s;
    border: 1px solid %(border_muted)s;
    border-radius: 10px;
    padding: 10px 14px;
}
#LangCard:hover {
    border-color: %(border)s;
    background: %(bg3)s;
}
#LangCard[selected="true"] {
    border-color: %(accent)s;
    background: %(accent_muted)s;
}

#ScreenInfoCard {
    background: %(bg2)s;
    border: 1px solid %(border_muted)s;
    border-radius: 10px;
    padding: 12px 16px;
}

/* === Projection Badge === */
#ProjectionBadge {
    background: %(projection)s;
    border-radius: 6px;
    padding: 3px 8px;
    color: white;
    font-size: 11px;
    font-weight: 700;
}

/* === Loading Spinner Label === */
#LoadingLabel {
    background: transparent;
    color: %(text_secondary)s;
    font-size: 13px;
}

/* === Tooltip === */
QToolTip {
    background-color: %(bg3)s;
    color: %(text_primary)s;
    border: 1px solid %(border)s;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
}

/* === ComboBox === */
QComboBox {
    background: %(bg2)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    padding: 7px 12px;
    color: %(text_primary)s;
    selection-background-color: %(accent_muted)s;
}
QComboBox:hover { border-color: %(accent)s; }
QComboBox::drop-down { width: 24px; border: none; }
QComboBox QAbstractItemView {
    background: %(bg2)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    selection-background-color: %(accent_muted)s;
}

/* === Menu === */
QMenu {
    background: %(bg2)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
    padding: 4px;
}
QMenu::item {
    padding: 8px 20px;
    border-radius: 6px;
    color: %(text_primary)s;
}
QMenu::item:selected {
    background: %(accent_muted)s;
    color: %(accent2)s;
}
""" % COLORS
