"""Compact Windows 11 inspired dark theme."""

STYLESHEET = r"""
QWidget { background: #11151c; color: #e8edf5; font-family: 'Segoe UI', 'Roboto', 'Helvetica', sans-serif; font-size: 10pt; }
QLabel { background: transparent; }
QMainWindow, QStackedWidget { background: #0d1117; }
QFrame#Card, QFrame#Header, QFrame#Inspector, QFrame#DropZone, QFrame#ImportCard, QFrame#ProgressPanel, QFrame#FilmstripSection { background: #161c25; border: 1px solid #263141; border-radius: 10px; }
QFrame#Header { background: #121923; border-color: #2a3648; }
QFrame#HeaderGroup { background: #0f151d; border: 1px solid #263244; border-radius: 8px; }
QFrame#HeaderGroup:hover { border-color: #3a4d67; }
QFrame#ToolStrip { background: transparent; border: none; }
QFrame#ToolStripGroup { background: #161c25; border: 1px solid #263141; border-radius: 10px; }
QFrame#ToolStripGroup:hover { border-color: #3a4d67; }
QLabel#ToolbarLabel { color: #8f9bad; font-size: 9pt; }
QFrame#ImportCard { background: #121923; border-color: #34445b; }
QLabel#ImportLogo { background: #0d131b; border: 1px solid #34465e; border-radius: 8px; }
QLabel#ImportTitle { color: #f5f8fd; font-size: 16pt; font-weight: 700; }
QLabel#ImportProjectName { color: #9ec2ff; background: #17243b; border: 1px solid #2f538d; border-radius: 6px; padding: 7px 10px; font-weight: 650; }
QFrame#ImportStages { background: transparent; border: none; }
QFrame#ImportStageRow { background: #0f151d; border: 1px solid #263244; border-radius: 8px; }
QFrame#ImportStageRow[stageState="active"] { background: #17243b; border-color: #4d83ff; }
QFrame#ImportStageRow[stageState="complete"] { background: #10251b; border-color: #244b35; }
QLabel#ImportStageMark { color: #8f9bad; font-weight: 700; }
QFrame#ImportStageRow[stageState="active"] QLabel#ImportStageMark { color: #9ec2ff; }
QFrame#ImportStageRow[stageState="complete"] QLabel#ImportStageMark { color: #7ee0a1; }
QLabel#ImportStageLabel { color: #aeb9c9; }
QLabel#ImportStageLabel[active="true"] { color: #f0f5ff; font-weight: 650; }
QProgressBar#ImportProgressBar { min-height: 12px; }
QLabel#ImportDetail { color: #a8b3c3; background: #0f151d; border: 1px solid #263244; border-radius: 7px; padding: 8px 10px; }
QFrame#ProgressPanel { background: #131a23; border-color: #26364a; }
QFrame#InspectorFooter { background: #111821; border: 1px solid #30405a; border-radius: 8px; }
QFrame#InspectorSection { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #151e2b, stop:1 #101720); border: 1px solid #30405a; border-radius: 8px; }
QLabel#InspectorSectionTitle { color: #f0f5ff; font-weight: 650; }
QDialog#WorkingDialog { background: #111821; border: 1px solid #3a5375; border-radius: 10px; }
QLabel#WorkingTitle { color: #f7fbff; font-size: 13pt; font-weight: 700; }
QTextEdit#WorkingLog { background: #0b1118; border: 1px solid #2a3b52; border-radius: 6px; color: #9fb8dc; font-family: Consolas, monospace; font-size: 9pt; padding: 6px; }
QToolButton#SectionToggle { background: transparent; border: none; color: #f0f5ff; padding: 8px 9px; font-weight: 650; text-align: left; }
QToolButton#SectionToggle:hover { background: #1c2a3d; border-radius: 7px; }
QFrame#DropZone { border: 2px dashed #3b4b61; background: #141b25; }
QFrame#DropZone[dragActive="true"] { border-color: #4d83ff; background: #17243b; }
QWidget#LandingContent, QWidget#RecentProjectsHost, QWidget#RecentProjectsDialogHost { background: transparent; }
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
QPushButton#ViewAllRecent { color: #9ec2ff; background: #17243b; border: 1px solid #2f538d; padding: 4px 9px; font-size: 9pt; }
QPushButton#ViewAllRecent:hover { color: #ffffff; background: #20345a; border-color: #4d83ff; }
QPushButton#ViewAllRecent:disabled { color: #526073; background: transparent; border-color: #263141; }
QScrollArea#RecentProjectsScroll { background: transparent; border: none; padding: 0; }
QScrollArea#RecentProjectsScroll > QWidget > QWidget { background: transparent; }
QScrollArea#RecentProjectsDialogScroll { background: transparent; border: none; padding: 0; }
QLineEdit#RecentSearch { padding: 8px 10px; font-size: 10pt; }
QFrame#RecentProjectCard { background: #151b24; border: 1px solid #2e3a4c; border-radius: 8px; }
QFrame#RecentProjectCard:hover { background: #192330; border-color: #466184; }
QFrame#RecentProjectCard[focused="true"] { background: #192330; border: 2px solid #4d83ff; }
QFrame#RecentIconTile { background: #0d131b; border: 1px solid #34465e; border-radius: 7px; }
QFrame#UpdateCard { background: #151b24; border: 1px solid #2e6b72; border-radius: 8px; }
QLabel#UpdateIcon { background: #12313a; border: 1px solid #2b8994; border-radius: 17px; }
QLabel#UpdateTitle { color: #eef3fa; font-weight: 650; }
QLabel#RecentProjectTitle { color: #eef3fa; font-weight: 650; }
QLabel#RecentProjectMeta { color: #a5b0bf; font-size: 9pt; }
QLabel#RecentMetaChip { color: #9ec2ff; background: #17243b; border: 1px solid #2f538d; border-radius: 5px; padding: 2px 6px; font-size: 8.5pt; }
QLabel#RecentOpened { color: #78a8e8; font-size: 9pt; }
QLabel#RecentCompatibilityWarning { color: #f0b35a; font-size: 9pt; }
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
QFrame#CanvasPanel { background: #0b1017; border: 1px solid #273244; border-radius: 8px; }
QFrame#CanvasPanelHeader { background: #121923; border: none; border-bottom: 1px solid #273244; border-top-left-radius: 8px; border-top-right-radius: 8px; }
QLabel#CanvasPanelTitle { color: #f0f5ff; font-weight: 650; }
QLabel#StatusPill { color: #7ee0a1; background: #10251b; border: 1px solid #244b35; border-radius: 5px; padding: 2px 7px; font-size: 8.5pt; }
QLabel#StatusPill[statusState="ready"], QLabel#StatusPill[statusState="translated"], QLabel#StatusPill[statusState="complete"] { color: #7ee0a1; background: #10251b; border-color: #244b35; }
QLabel#StatusPill[statusState="review"], QLabel#StatusPill[statusState="partial"], QLabel#StatusPill[statusState="queued"], QLabel#StatusPill[statusState="pending"] { color: #ffcc66; background: #2b2413; border-color: #6b5420; }
QLabel#StatusPill[statusState="failed"] { color: #ff8a8f; background: #2d151b; border-color: #7c2d36; }
QLabel#StatusPill[statusState="cancelled"] { color: #c4cfdd; background: #1b222d; border-color: #3a4656; }
QLabel#StatusPill[statusState="ocr"], QLabel#StatusPill[statusState="translating"], QLabel#StatusPill[statusState="rendering"], QLabel#StatusPill[statusState="reconstructing"] { color: #9ec2ff; background: #17243b; border-color: #2f538d; }
QToolButton#IdentityTile { background: #151c26; border: 1px solid transparent; border-radius: 6px; color: #d7deea; padding: 4px; }
QToolButton#IdentityTile:hover { background: #1c2735; border-color: #34465e; }
QToolButton#IdentityTile:checked { background: #1d355b; border: 2px solid #4d83ff; color: #ffffff; }
QToolButton#SpeechButton { background: #17243b; border: 1px solid #315fbc; border-radius: 5px; padding: 0; }
QToolButton#SpeechButton:hover { background: #20345a; border-color: #4d83ff; }
QToolButton#SpeechButton:disabled { background: #141d2b; border-color: #284877; }
QLabel#PipelineStages { color: #93a4ba; font-size: 9pt; }
QLabel#ImportSummary { color: #b9c7da; background: #111720; border: 1px solid #26364a; border-radius: 6px; padding: 10px; }
QLabel[active="true"] { color: #78a8ff; font-weight: 600; }
QLabel#CanvasBadge { background: rgba(12, 17, 24, 210); color: #f2f6fc; border: 1px solid #344156; border-radius: 5px; padding: 4px 7px; font-size: 9pt; }
QPushButton { background: #202a38; border: 1px solid #303d50; border-radius: 6px; padding: 7px 18px 7px 13px; }
QPushButton:hover { background: #29364a; border-color: #4b6382; }
QPushButton#ToolIconButton { min-width: 30px; max-width: 34px; padding: 7px 0; font-weight: 700; }
QToolButton#ToolbarButton { background: #202a38; border: 1px solid #303d50; border-radius: 6px; padding: 7px 18px 7px 13px; color: #e8edf5; }
QToolButton#ToolbarButton:hover, QToolButton#ToolbarButton:pressed, QToolButton#ToolbarButton:checked { background: #29364a; border-color: #4b6382; }
QToolButton#ToolbarButton:disabled { color: #667080; background: #171d26; }
QToolButton#ToolbarButton::menu-indicator { subcontrol-origin: padding; subcontrol-position: right center; right: 5px; }
QPushButton#Primary { background: #2864dc; border-color: #3676ed; color: white; font-weight: 600; }
QPushButton#Primary:hover { background: #3474ef; }
QPushButton#InspectorPrimary { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2864dc, stop:1 #2476ff); border: 1px solid #4b8cff; color: white; font-weight: 700; padding: 9px 13px; }
QPushButton#InspectorPrimary:hover { background: #3474ef; border-color: #78a8ff; }
QPushButton#DangerButton { color: #ff7272; }
QPushButton#DangerButton:hover { color: #ff9494; border-color: #7c3d47; background: #2b1e28; }
QPushButton:disabled { color: #667080; background: #171d26; }
QLineEdit, QTextEdit, QSpinBox, QComboBox, QListWidget, QTabWidget::pane { background: #0f141b; border: 1px solid #293547; border-radius: 5px; padding: 5px; }
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus, QListWidget:focus { border-color: #4d83ff; }
QListWidget::item { padding: 7px; border-bottom: 1px solid #202938; }
QListWidget::item:focus { border-color: #4d83ff; background: #1f3a63; }
QListWidget::item:selected { background: #234f9d; }
QListWidget#TextBlocksList { background: #0d131b; border: 1px solid #30405a; border-radius: 8px; padding: 6px; }
QListWidget#TextBlocksList::item { padding: 8px 10px; border-bottom: 1px solid #223049; }
QListWidget#TextBlocksList::item:selected { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2468dc, stop:1 #1c4da8); color: #ffffff; }
QListWidget#Filmstrip { background: transparent; border: none; border-radius: 0; padding: 0; }
QListWidget#Filmstrip::item { background: #151c26; border: 1px solid #202d3e; border-radius: 6px; padding: 4px; }
QListWidget#Filmstrip::item:hover { background: #1c2735; border-color: #34465e; }
QListWidget#Filmstrip::item:selected { background: #1d355b; border: 2px solid #4d83ff; }
QTabBar::tab { background: #161c25; padding: 8px 15px; color: #9ba7b8; }
QTabBar::tab:selected { color: #69a0ff; border-bottom: 2px solid #4d83ff; }
QProgressBar { background: #171d27; border: 1px solid #263141; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #3478ed; border-radius: 4px; }
QScrollBar:vertical, QScrollBar:horizontal { background: #111720; width: 10px; height: 10px; }
QScrollBar::handle { background: #344156; border-radius: 4px; min-height: 20px; min-width: 20px; }
QScrollBar::handle:hover { background: #4a5c78; }
QGraphicsView { background: #090c11; border: 1px solid #263141; border-radius: 7px; }
QStatusBar { background: #0d1117; color: #8793a5; }
"""
