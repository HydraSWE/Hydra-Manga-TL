"""Compact Windows 11 inspired dark theme."""

STYLESHEET = r"""
QWidget { background: #11151c; color: #e8edf5; font-family: 'Segoe UI'; font-size: 10pt; }
QLabel { background: transparent; }
QMainWindow, QStackedWidget { background: #0d1117; }
QFrame#Card, QFrame#Header, QFrame#Inspector, QFrame#DropZone, QFrame#ImportCard, QFrame#ProgressPanel { background: #161c25; border: 1px solid #263141; border-radius: 10px; }
QFrame#ImportCard { border-color: #34445b; }
QFrame#ProgressPanel { background: #131a23; border-color: #26364a; }
QFrame#InspectorFooter { background: #111821; border: 1px solid #30405a; border-radius: 8px; }
QFrame#InspectorSection { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #151e2b, stop:1 #101720); border: 1px solid #30405a; border-radius: 8px; }
QLabel#InspectorSectionTitle { color: #f0f5ff; font-weight: 650; }
QDialog#WorkingDialog { background: #111821; border: 1px solid #3a5375; border-radius: 10px; }
QLabel#WorkingTitle { color: #f7fbff; font-size: 13pt; font-weight: 700; }
QTextEdit#WorkingLog { background: #0b1118; border: 1px solid #2a3b52; border-radius: 6px; color: #9fb8dc; font-family: Consolas, monospace; font-size: 9pt; padding: 6px; }
QToolButton#SectionToggle { background: transparent; border: none; color: #f0f5ff; padding: 8px 9px; font-weight: 650; text-align: left; }
QToolButton#SectionToggle:hover { background: #1c2a3d; border-radius: 7px; }
QFrame#DropZone { border: 2px dashed #3b4b61; }
QFrame#DropZone[dragActive="true"] { border-color: #4d83ff; background: #17243b; }
QWidget#LandingContent, QWidget#RecentProjectsHost { background: transparent; }
QLabel#LandingHeroTitle { color: #f5f8fd; font-size: 17pt; font-weight: 650; }
QLabel#LandingDescription { color: #a8b3c3; font-size: 10.5pt; }
QLabel#DropTitle { color: #f3f7fc; font-size: 15pt; font-weight: 650; }
QLabel#DropMeta { color: #8291a5; font-size: 9pt; }
QLabel#ActionDivider { color: #4a5b72; padding: 0 2px; }
QLabel#RecentHeading { color: #f5f8fd; font-size: 15pt; font-weight: 650; }
QPushButton#LandingPrimary { background: #2864dc; border-color: #4382f2; color: white; font-size: 11pt; font-weight: 650; padding: 9px 22px; }
QPushButton#LandingPrimary:hover { background: #3474ef; border-color: #5b94ff; }
QPushButton#SecondaryLink { color: #69a0ff; background: transparent; border: none; padding: 3px 7px; }
QPushButton#SecondaryLink:hover { color: #91bbff; text-decoration: underline; }
QPushButton#ClearHistory { color: #8f9bad; background: transparent; border: none; padding: 4px 6px; font-size: 9pt; }
QPushButton#ClearHistory:hover { color: #d7e2f0; background: #1c2531; }
QPushButton#ClearHistory:disabled { color: #526073; background: transparent; }
QScrollArea#RecentProjectsScroll { background: transparent; border: none; padding: 0; }
QScrollArea#RecentProjectsScroll > QWidget > QWidget { background: transparent; }
QFrame#RecentProjectCard { background: #151b24; border: 1px solid #2e3a4c; border-radius: 8px; }
QFrame#RecentProjectCard:hover { background: #192330; border-color: #466184; }
QFrame#RecentProjectCard[focused="true"] { background: #192330; border: 2px solid #4d83ff; }
QFrame#RecentIconTile { background: #192331; border: 1px solid #34465e; border-radius: 7px; }
QLabel#RecentProjectTitle { color: #eef3fa; font-weight: 650; }
QLabel#RecentProjectMeta { color: #a5b0bf; font-size: 9pt; }
QLabel#RecentOpened { color: #78a8e8; font-size: 9pt; }
QPushButton#RecentRemove { color: #7f8da0; background: transparent; border: none; border-radius: 4px; padding: 0; font-size: 13pt; }
QPushButton#RecentRemove:hover { color: #ffffff; background: #34445b; }
QLabel#EmptyRecent { color: #78879a; background: #111720; border: 1px dashed #2c394b; border-radius: 8px; }
QLabel#Brand { font-size: 20pt; font-weight: 700; color: white; }
QLabel#Heading { font-size: 15pt; font-weight: 650; color: white; }
QLabel#Muted { color: #8f9bad; }
QLabel#JobTitle { color: #dce7f7; font-weight: 650; }
QToolButton#JobToggle { background: transparent; border: none; padding: 2px; color: #9fb0c6; }
QToolButton#JobToggle:hover { background: #202b3a; border-radius: 4px; }
QToolButton#JobToggle:disabled { color: #526073; }
QToolButton#SpeechButton { background: #17243b; border: 1px solid #315fbc; border-radius: 5px; padding: 0; }
QToolButton#SpeechButton:hover { background: #20345a; border-color: #4d83ff; }
QToolButton#SpeechButton:disabled { background: #141d2b; border-color: #284877; }
QLabel#PipelineStages { color: #93a4ba; font-size: 9pt; }
QLabel#ImportSummary { color: #b9c7da; background: #111720; border: 1px solid #26364a; border-radius: 6px; padding: 10px; }
QLabel[active="true"] { color: #78a8ff; font-weight: 600; }
QLabel#CanvasBadge { background: rgba(12, 17, 24, 210); color: #f2f6fc; border: 1px solid #344156; border-radius: 5px; padding: 4px 7px; font-size: 9pt; }
QPushButton { background: #202a38; border: 1px solid #303d50; border-radius: 6px; padding: 7px 13px; }
QPushButton:hover { background: #29364a; border-color: #4b6382; }
QPushButton#Primary { background: #2864dc; border-color: #3676ed; color: white; font-weight: 600; }
QPushButton#Primary:hover { background: #3474ef; }
QPushButton#InspectorPrimary { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2864dc, stop:1 #2476ff); border: 1px solid #4b8cff; color: white; font-weight: 700; padding: 9px 13px; }
QPushButton#InspectorPrimary:hover { background: #3474ef; border-color: #78a8ff; }
QPushButton#DangerButton { color: #ff7272; }
QPushButton#DangerButton:hover { color: #ff9494; border-color: #7c3d47; background: #2b1e28; }
QPushButton:disabled { color: #667080; background: #171d26; }
QLineEdit, QTextEdit, QSpinBox, QComboBox, QListWidget, QTabWidget::pane { background: #0f141b; border: 1px solid #293547; border-radius: 5px; padding: 5px; }
QListWidget::item { padding: 7px; border-bottom: 1px solid #202938; }
QListWidget::item:selected { background: #234f9d; }
QListWidget#TextBlocksList { background: #0d131b; border: 1px solid #30405a; border-radius: 8px; padding: 6px; }
QListWidget#TextBlocksList::item { padding: 8px 10px; border-bottom: 1px solid #223049; }
QListWidget#TextBlocksList::item:selected { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2468dc, stop:1 #1c4da8); color: #ffffff; }
QListWidget#Filmstrip { padding: 4px; }
QListWidget#Filmstrip::item { background: #151c26; border: 1px solid transparent; border-radius: 6px; padding: 4px; }
QListWidget#Filmstrip::item:hover { background: #1c2735; border-color: #34465e; }
QListWidget#Filmstrip::item:selected { background: #1d355b; border: 2px solid #4d83ff; }
QTabBar::tab { background: #161c25; padding: 8px 15px; color: #9ba7b8; }
QTabBar::tab:selected { color: #69a0ff; border-bottom: 2px solid #4d83ff; }
QProgressBar { background: #171d27; border: 1px solid #263141; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #3478ed; border-radius: 4px; }
QScrollBar:vertical, QScrollBar:horizontal { background: #111720; width: 10px; height: 10px; }
QScrollBar::handle { background: #344156; border-radius: 4px; min-height: 20px; min-width: 20px; }
QGraphicsView { background: #090c11; border: 1px solid #263141; border-radius: 7px; }
QStatusBar { background: #0d1117; color: #8793a5; }
"""
