"""Gallery Monitor v80 — PySide6 desktop interface.

The v80 monitoring backend (main.py), configuration keys, Telegram message
formats, and SQLite schema are deliberately unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QStyle, QSystemTrayIcon, QMenu,
    QStyleOptionButton, QVBoxLayout, QWidget,
)

from database_viewer import DatabaseViewer
from main import BASE_DIR, CONFIG_PATH, DB_PATH, DEFAULT_CONFIG, Monitor
from app_icon import load_app_icon


STYLE = """
QWidget { color: #16243A; font-family: 'Malgun Gothic', 'Segoe UI', sans-serif; font-size: 9pt; }
QWidget#root { background: #F5F7FF; }
QWidget#sidebar { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #EEF0FF,stop:1 #F8F6FF); border-right: 1px solid #DFE4F8; }
QWidget#content { background: #F6F8FF; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QFrame#card, QFrame#metric, QFrame#logCard {
    background: #FFFFFF; border: 1px solid #E1E6F6; border-radius: 12px;
}
QFrame#statusPill { background: #E5FAF2; border: 1px solid #BEEDD8; border-radius: 11px; }
QLabel#pillText { color: #08784E; font-size: 9pt; font-weight: 700; }
QLabel#muted { color: #73829A; font-size: 9pt; }
QLabel#sectionTitle { color: #1C2940; font-size: 11pt; font-weight: 700; }
QLabel#pageTitle { color: #10213B; font-size: 18pt; font-weight: 800; }
QLabel#metricValue { font-size: 15pt; font-weight: 800; color: #152C51; }
QLabel#fieldLabel { color: #3B4C65; font-weight: 600; font-size: 9pt; }
QLabel#brandTitle { color: #192848; font-size: 13pt; font-weight: 800; }
QLabel#brandSub { color: #7784A5; font-size: 9pt; }
QLabel#sideFoot { color: #8792B2; font-size: 9pt; }
QPushButton { border-radius: 9px; padding: 7px 11px; font-weight: 700; }
QPushButton#navBtn {
    text-align: left; color: #51617F; background: transparent;
    border: 0; border-left: 3px solid transparent; padding: 9px 13px;
}
QPushButton#navBtn:hover { background: #E0E6FF; color: #3448B8; }
QPushButton#navBtn:checked { background: #777DF2; color: #FFFFFF; border-left: 3px solid #585CE0; }
QPushButton#primary { color: #FFFFFF; background: #6D7FF0; border: 1px solid #6D7FF0; }
QPushButton#primary:hover { background: #5269D4; }
QPushButton#primary:disabled { background: #B4C7E5; border-color: #B4C7E5; }
QPushButton#stop { color: #C14760; background: #FFF0F4; border: 1px solid #F5CFDA; }
QPushButton#stop:hover { background: #FFE2EB; }
QPushButton#stop:disabled { color: #A5B1C2; background: #F2F5F9; border-color: #E7EAF0; }
QPushButton#secondary { color: #314664; background: #FFFFFF; border: 1px solid #DBE4F0; }
QPushButton#secondary:hover { background: #F2F6FD; border-color: #ACBFDA; }
QPushButton#ghost { color: #5665C4; background: #F0F1FF; border: 1px solid #E0E4FD; }
QPushButton#ghost:hover { background: #E4E8FF; }
QLineEdit, QSpinBox {
    background: #F9FBFE; color: #152840; border: 1px solid #DCE5F0;
    border-radius: 7px; padding: 7px 9px; selection-background-color: #8AB9FF;
    min-height: 17px;
}
QLineEdit:focus, QSpinBox:focus { border: 1px solid #4285EE; background: #FFFFFF; }
QLineEdit:disabled, QSpinBox:disabled { color: #96A3B5; background: #F1F3F6; }
QSpinBox { padding-right: 3px; }
QCheckBox { spacing: 7px; color: #34435D; }
/* Draw the check mark ourselves so Windows accent/theme cannot make it red. */
QCheckBox::indicator {
    width: 17px; height: 17px; border: none; background: transparent;
}
QPlainTextEdit {
    background: #121F34; color: #BEE0E8; border: none; border-radius: 11px;
    padding: 10px 12px; font-family: 'Consolas', 'D2Coding', monospace; font-size: 9pt;
    selection-background-color: #32577A;
}
QScrollBar:vertical { width: 10px; background: transparent; }
QScrollBar::handle:vertical { background: #C2CCDA; min-height: 28px; border-radius: 5px; margin: 2px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { height: 9px; background: transparent; }
QScrollBar::handle:horizontal { background: #C2CCDA; min-width: 28px; border-radius: 4px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""


class NoWheelSpinBox(QSpinBox):
    """Let the surrounding page scroll without changing numbers under the cursor.

    Values still change normally through the spin buttons and keyboard.
    """

    def wheelEvent(self, event) -> None:
        event.ignore()  # Hand the wheel gesture to the parent scroll area.


class MonitoringCancelled(BaseException):
    """Cooperative cancellation that cannot be swallowed by v80's except Exception."""


class CancellableMonitor(Monitor):
    """Preserve v80's monitoring engine, interrupting between network operations."""

    def __init__(self, config: dict[str, Any], stop_event: threading.Event):
        self._stop_event = stop_event
        if stop_event.is_set():
            raise MonitoringCancelled()
        super().__init__(config)

    def _check_stop(self) -> None:
        if self._stop_event.is_set():
            raise MonitoringCancelled()

    def get_html(self, url: str) -> str:
        self._check_stop()
        result = super().get_html(url)
        self._check_stop()
        return result

    def get_comments(self, post):
        self._check_stop()
        result = super().get_comments(post)
        self._check_stop()
        return result

    def send_telegram(self, text: str) -> None:
        self._check_stop()
        # Let the caller record a successfully sent message before cancelling.
        super().send_telegram(text)


class SoftCheckBox(QCheckBox):
    """Theme-independent lavender checkbox with a clearly visible white tick.

    The original widget used the OS-native indicator, whose accent color can
    differ between Windows machines.  Only the indicator is custom-painted;
    Qt still handles focus, keyboard toggling, accessibility and label text.
    """

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator, option, self
        )
        if rect.isEmpty():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(rect).adjusted(1.0, 1.0, -1.0, -1.0)
        checked = self.checkState() != Qt.CheckState.Unchecked
        if not self.isEnabled():
            fill, edge = '#E7EBF2', '#D3DAE6'
        elif checked:
            fill, edge = '#6D7FF0', '#5B6FE3'
        else:
            fill = '#F6F8FF' if self.underMouse() else '#FFFFFF'
            edge = '#788BDF' if self.underMouse() else '#C7D2E5'

        painter.setBrush(QColor(fill))
        painter.setPen(QPen(QColor(edge), 1.2))
        painter.drawRoundedRect(box, 4.5, 4.5)

        if checked:
            color = QColor('#FFFFFF' if self.isEnabled() else '#9CA9C0')
            painter.setPen(QPen(
                color, 2.2, Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
            ))
            x, y, w, h = box.x(), box.y(), box.width(), box.height()
            if self.checkState() == Qt.CheckState.PartiallyChecked:
                painter.drawLine(QPointF(x + w * .27, y + h * .5),
                                 QPointF(x + w * .73, y + h * .5))
            else:
                painter.drawPolyline(QPolygonF([
                    QPointF(x + w * .23, y + h * .51),
                    QPointF(x + w * .43, y + h * .70),
                    QPointF(x + w * .77, y + h * .31),
                ]))
        painter.end()


class QueueLogHandler(logging.Handler):
    """Logging threads only enqueue strings; UI widgets are updated on Qt's GUI thread."""

    def __init__(self, messages: queue.Queue[str]):
        super().__init__()
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)


class HeroCard(QFrame):
    """Painted Qt gradient header, deliberately not dependent on external images."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(118)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        box = QVBoxLayout(self)
        box.setContentsMargins(20, 14, 20, 13)
        box.setSpacing(5)
        eyebrow = QLabel('GALLERY MONITOR  /  V80 UI')
        eyebrow.setStyleSheet('color:#EEF4FF; font-size:9pt; font-weight:700; background:transparent;')
        box.addWidget(eyebrow)
        title = QLabel('갤러리를 더 편하게, 알림은 더 빠르게.')
        title.setStyleSheet('color:#FFFFFF; font-size:15pt; font-weight:800; background:transparent;')
        box.addWidget(title)
        caption = QLabel('실시간 감지 · 맞춤형 알림 · 텔레그램 연동을 한 화면에서 관리하세요.')
        caption.setWordWrap(True)
        caption.setStyleSheet('color:#F4F8FF; font-size:9pt; background:transparent;')
        box.addWidget(caption)
        box.addStretch()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor('#7C88EC'))
        gradient.setColorAt(0.62, QColor('#849DF5'))
        gradient.setColorAt(1, QColor('#8ACEDF'))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(gradient)
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 18, 18)
        p.setBrush(QColor(255, 255, 255, 13))
        p.drawEllipse(self.width() - 220, -100, 330, 330)
        p.setBrush(QColor(115, 237, 220, 20))
        p.drawEllipse(self.width() - 110, 44, 180, 180)
        p.end()


class MonitorApp(QMainWindow):
    PAGES = (
        ('⌂', '대시보드', '현재 상태와 모니터링 설정을 관리합니다.'),
        ('◎', '알림 규칙', '알림 종류와 감지 키워드를 설정합니다.'),
        ('➤', '텔레그램', '봇 연결과 테스트 메시지를 관리합니다.'),
        ('▤', '데이터 기록', '수집 및 알림 전송 이력을 조회합니다.'),
    )

    def __init__(self):
        super().__init__()
        self.setWindowTitle('🐧 갤러리 모니터 v80  |  by 남극좌')
        self.setWindowIcon(load_app_icon())
        self.resize(1160, 760)
        self.setMinimumSize(950, 610)
        self.stop_event = threading.Event()
        self.workers: list[threading.Thread] = []
        self._stopping = False
        self._restart_pending = False
        self._allow_close = False
        self.tray_icon = None
        self.messages: queue.Queue[str] = queue.Queue()
        self.logger = logging.getLogger('retrogame_monitor')
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        handler = QueueLogHandler(self.messages)
        handler.setFormatter(logging.Formatter('%(asctime)s  %(message)s', '%H:%M:%S'))
        self.logger.addHandler(handler)
        self.logger.propagate = False
        self.loaded_config = self._load_existing_config()
        self._build_ui()
        self._setup_system_tray()
        self._update_status('중지됨')
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self._drain_messages)
        self.log_timer.start(150)
        self._show_first_run_documents()
        # Same v80 startup behavior: a single topic check on launch, if configured.
        QTimer.singleShot(500, self._initial_topic_check)

    @staticmethod
    def _load_existing_config() -> dict[str, Any]:
        config = dict(DEFAULT_CONFIG)
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    config.update(loaded)
            except (OSError, json.JSONDecodeError):
                pass
        return config

    @staticmethod
    def _label(text: str, object_name: str = '') -> QLabel:
        label = QLabel(text)
        if object_name:
            label.setObjectName(object_name)
        return label

    @staticmethod
    def _line(value: str = '', placeholder: str = '', secret: bool = False) -> QLineEdit:
        field = QLineEdit(value)
        field.setPlaceholderText(placeholder)
        field.setClearButtonEnabled(not secret)
        if secret:
            field.setEchoMode(QLineEdit.EchoMode.Password)
        return field

    @staticmethod
    def _spin(value: Any, maximum: int = 2147483647, suffix: str = '') -> QSpinBox:
        field = NoWheelSpinBox()
        field.setRange(1, maximum)
        try:
            field.setValue(int(value))
        except (TypeError, ValueError):
            field.setValue(1)
        if suffix:
            field.setSuffix(f' {suffix}')
        field.setMinimumWidth(105)
        return field

    @staticmethod
    def _button(text: str, style: str, callback) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(style)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    @staticmethod
    def _card(title: str, subtitle: str = '') -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName('card')
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 13, 16, 15)
        layout.setSpacing(10)
        layout.addWidget(MonitorApp._label(title, 'sectionTitle'))
        if subtitle:
            helper = MonitorApp._label(subtitle, 'muted')
            helper.setWordWrap(True)
            layout.addWidget(helper)
        return card, layout

    @staticmethod
    def _field(label: str, widget: QWidget, hint: str = '') -> QWidget:
        wrapper = QWidget()
        column = QVBoxLayout(wrapper)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(MonitorApp._label(label, 'fieldLabel'))
        column.addWidget(widget)
        if hint:
            help_label = MonitorApp._label(hint, 'muted')
            help_label.setWordWrap(True)
            column.addWidget(help_label)
        return wrapper

    @staticmethod
    def _grid_container() -> tuple[QWidget, QGridLayout]:
        box = QWidget()
        layout = QGridLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(11)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return box, layout

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName('root')
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(196)
        shell.addWidget(sidebar)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(10, 19, 10, 16)
        side.setSpacing(5)
        brand = QVBoxLayout()
        brand.setContentsMargins(10, 0, 0, 14)
        brand.setSpacing(4)
        brand.addWidget(self._label('🐧  갤러리 모니터', 'brandTitle'))
        brand.addWidget(self._label('GALLERY MONITOR  ·  v80', 'brandSub'))
        side.addLayout(brand)
        label = self._label('WORKSPACE', 'sideFoot')
        label.setContentsMargins(12, 0, 0, 7)
        side.addWidget(label)
        self.nav_buttons: list[QPushButton] = []
        for index, (icon, name, _) in enumerate(self.PAGES):
            button = self._button(f' {icon}    {name}', 'navBtn', lambda checked=False, i=index: self._navigate(i))
            button.setCheckable(True)
            button.setMinimumHeight(42)
            self.nav_buttons.append(button)
            side.addWidget(button)
        side.addStretch()
        side.addWidget(self._label('도움말', 'sideFoot'))
        side.addWidget(self._button('📖  사용 안내 (readme)', 'navBtn', lambda: self.open_document('readme.txt')))
        side.addWidget(self._button('📝  업데이트 (update)', 'navBtn', lambda: self.open_document('update.txt')))
        side.addSpacing(6)
        side.addWidget(self._label('SYSTEM', 'sideFoot'))
        side.addWidget(self._label('●  로컬 SQLite 기록', 'brandSub'))
        side.addWidget(self._label('●  Telegram 알림 연동', 'brandSub'))
        side.addSpacing(6)
        side.addWidget(self._label('v80  /  PySide6 Edition', 'sideFoot'))

        content = QWidget()
        content.setObjectName('content')
        shell.addWidget(content, 1)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 16, 20, 15)
        content_layout.setSpacing(12)
        top = QHBoxLayout()
        left = QVBoxLayout()
        left.setSpacing(2)
        self.page_title = self._label('', 'pageTitle')
        left.addWidget(self.page_title)
        self.page_subtitle = self._label('', 'muted')
        left.addWidget(self.page_subtitle)
        top.addLayout(left)
        top.addStretch()
        status_frame = QFrame()
        status_frame.setObjectName('statusPill')
        pill = QHBoxLayout(status_frame)
        pill.setContentsMargins(11, 5, 11, 5)
        self.status_text = self._label('●  중지됨', 'pillText')
        pill.addWidget(self.status_text)
        top.addWidget(status_frame)
        top.addSpacing(4)
        self.start_button = self._button('▶  시작', 'primary', self.start)
        self.stop_button = self._button('■  중지', 'stop', self.stop)
        self.save_button = self._button('설정 저장', 'secondary', self.save_config)
        self.stop_button.setEnabled(False)
        for button in (self.start_button, self.stop_button, self.save_button):
            top.addWidget(button)
        content_layout.addLayout(top)
        self.pages = QStackedWidget()
        content_layout.addWidget(self.pages, 1)
        self.pages.addWidget(self._scroll_page(self._build_dashboard()))
        self.pages.addWidget(self._scroll_page(self._build_alerts()))
        self.pages.addWidget(self._scroll_page(self._build_telegram()))
        self.db_viewer = DatabaseViewer()
        self.pages.addWidget(self.db_viewer)
        self._navigate(0)

    @staticmethod
    def _scroll_page(inner: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _make_page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(1, 1, 6, 6)
        layout.setSpacing(11)
        return page, layout

    def _metric(self, title: str, value: str, subtitle: str) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName('metric')
        layout = QVBoxLayout(card)
        layout.setContentsMargins(15, 12, 14, 12)
        layout.setSpacing(7)
        title_label = self._label(title, 'muted')
        title_label.setWordWrap(True)
        layout.addWidget(title_label)
        value_label = self._label(value, 'metricValue')
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(value_label)
        subtitle_label = self._label(subtitle, 'muted')
        subtitle_label.setWordWrap(True)  # Keep original text visible in the narrower cards.
        layout.addWidget(subtitle_label)
        return card, value_label

    def _build_dashboard(self) -> QWidget:
        cfg = self.loaded_config
        page, layout = self._make_page()
        layout.addWidget(HeroCard())
        # Preserve existing controls and hero; move only the log to the right.
        columns = QHBoxLayout()
        columns.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(11)
        metrics = QHBoxLayout()
        metrics.setSpacing(10)
        metric1, self.run_metric = self._metric('모니터링 상태', '대기 중', '실행 버튼으로 시작')
        metric2, self.interval_metric = self._metric('키워드 검사', f'{cfg["keyword_interval_minutes"]}분', '주기적인 게시글 감지')
        metric3, self.posts_metric = self._metric('최근 글 확인', f'{cfg["recent_posts_to_scan"]}개', '최대 200개까지 설정 가능')
        for metric in (metric1, metric2, metric3):
            metrics.addWidget(metric, 1)
        left.addLayout(metrics)
        card, content = self._card('모니터링 기본 설정', '게시판과 조회 빈도를 지정해 주세요. 변경한 값은 저장하거나 시작할 때 적용됩니다.')
        self.board_edit = self._line(str(cfg.get('board_url', '')), 'https://gall.dcinside.com/mgallery/board/lists/?id=retrogame')
        content.addWidget(self._field('모니터링 게시판 주소', self.board_edit))
        gridbox, grid = self._grid_container()
        self.topic_spin = self._spin(cfg['topic_interval_minutes'], suffix='분')
        self.keyword_spin = self._spin(cfg['keyword_interval_minutes'], suffix='분')
        self.new_title_spin = self._spin(cfg.get('new_title_interval_minutes', 5), suffix='분')
        self.posts_spin = self._spin(cfg.get('recent_posts_to_scan', 30), 200, '개')
        self.request_gap_spin = self._spin(cfg.get('request_gap_seconds', 5), suffix='초')
        grid.addWidget(self._field('1페이지 분석 주기', self.topic_spin), 0, 0)
        grid.addWidget(self._field('키워드 검사 주기', self.keyword_spin), 0, 1)
        grid.addWidget(self._field('새 글 모아보기 주기', self.new_title_spin), 1, 0)
        grid.addWidget(self._field('최근 게시글 확인 수 (1~200)', self.posts_spin), 1, 1)
        grid.addWidget(self._field('게시판 요청 간격 (1초 이상)', self.request_gap_spin, '서버에 무리가 가지 않도록 여유 있게 지정해 주세요.'), 2, 0)
        content.addWidget(gridbox)
        left.addWidget(card)
        left.addStretch()
        columns.addLayout(left, 11)

        log_card, log_layout = self._card('실시간 실행 로그', '모니터링 활동과 오류가 시간순으로 표시됩니다.')
        clear_row = QHBoxLayout()
        clear_row.addStretch()
        clear_row.addWidget(self._button('로그 지우기', 'ghost', self._clear_logs))
        log_layout.addLayout(clear_row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(360)
        self.log_view.document().setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText('모니터링 로그가 이곳에 표시됩니다.')
        log_layout.addWidget(self.log_view)
        log_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        columns.addWidget(log_card, 9)
        layout.addLayout(columns, 1)
        self.keyword_spin.valueChanged.connect(lambda n: self.interval_metric.setText(f'{n}분'))
        self.posts_spin.valueChanged.connect(lambda n: self.posts_metric.setText(f'{n}개'))
        return page

    def _alert_count_row(self, parent: QVBoxLayout, title: str, enabled: bool, count: Any, label: str) -> tuple[QCheckBox, QSpinBox]:
        row = QHBoxLayout()
        row.setSpacing(9)
        check = SoftCheckBox(title)
        check.setChecked(enabled)
        count_field = self._spin(count, suffix='개')
        count_field.setToolTip(label + ' 묶음 개수')
        count_field.setMaximumWidth(124)
        count_field.setEnabled(check.isChecked())
        check.toggled.connect(count_field.setEnabled)
        row.addWidget(check)
        row.addStretch()
        row.addWidget(count_field)
        parent.addLayout(row)
        return check, count_field

    def _build_alerts(self) -> QWidget:
        cfg = self.loaded_config
        page, layout = self._make_page()
        summary = self._label('조건에 맞는 게시글과 댓글만 골라 Telegram으로 전송합니다.', 'muted')
        layout.addWidget(summary)
        both, grid = self._grid_container()
        card1, c1 = self._card('게시글 알림', '모아보기 알림을 켜거나 각 묶음 개수를 조절하세요.')
        self.topic_check, self.topic_count = self._alert_count_row(c1, '1페이지 분석 알림', cfg.get('topic_alert_enabled', True), cfg.get('topic_batch_count', 30), '새 글')
        self.new_title_check, self.new_title_count = self._alert_count_row(c1, '새 글 모아보기', cfg.get('new_title_alert_enabled', True), cfg.get('new_title_batch_count', 10), '새 글')
        self.newbie_title_check, self.newbie_title_count = self._alert_count_row(c1, '뉴비 새 글 모아보기', cfg.get('newbie_title_alert_enabled', True), cfg.get('newbie_title_batch_count', 5), '뉴비')
        self.concept_check, self.concept_count = self._alert_count_row(c1, '개념글 모아보기', cfg.get('concept_alert_enabled', True), cfg.get('concept_batch_count', 3), '개념글')
        c1.addStretch()
        grid.addWidget(card1, 0, 0)
        card2, c2 = self._card('추가 감지 기능', '필요한 탐지 유형만 활성화해 두세요.')
        self.link_check = SoftCheckBox('링크가 있는 본문·댓글 알림')
        self.link_check.setChecked(bool(cfg.get('include_embedded_links', True)))
        c2.addWidget(self.link_check)
        self.newbie_check = SoftCheckBox('뉴비 작성자 아이콘 표시 👶')
        self.newbie_check.setChecked(bool(cfg.get('newbie_alert_enabled', True)))
        c2.addWidget(self.newbie_check)
        self.profanity_check = SoftCheckBox('욕설 댓글 감지 알림 🤬')
        self.profanity_check.setChecked(bool(cfg.get('profanity_alert_enabled', False)))
        c2.addWidget(self.profanity_check)
        self.comment_check, self.comment_count = self._alert_count_row(c2, '댓글 모아보기', cfg.get('comment_batch_alert_enabled', False), cfg.get('comment_batch_count', 50), '댓글')
        c2.addStretch()
        grid.addWidget(card2, 0, 1)
        layout.addWidget(both)
        keyword_card, key = self._card('키워드 필터', '여러 키워드는 쉼표(,)로 구분해 주세요. 대소문자 구분 없이 탐지합니다.')
        self.title_keywords_edit = self._line(', '.join(cfg.get('title_keywords', cfg.get('keywords', []))), '예: 한글패치, 게임기, 세이브')
        self.body_keywords_edit = self._line(', '.join(cfg.get('body_keywords', [])), '예: 공략, 배포, 다운로드')
        self.comment_keywords_edit = self._line(', '.join(cfg.get('comment_keywords', [])), '예: 정보, 질문, 도움')
        for name, field, hint in (
            ('제목 키워드', self.title_keywords_edit, '게시글 제목에서 감지합니다.'),
            ('본문 키워드', self.body_keywords_edit, '게시글 본문에서 감지합니다.'),
            ('댓글 키워드', self.comment_keywords_edit, '댓글 내용에서 감지합니다.'),
        ):
            key.addWidget(self._field(name, field, hint))
        layout.addWidget(keyword_card)
        layout.addStretch()
        return page

    def _build_telegram(self) -> QWidget:
        cfg = self.loaded_config
        page, layout = self._make_page()
        card, content = self._card('텔레그램 봇 연결', 'BotFather에서 발급받은 토큰과 알림을 받을 Chat ID를 입력하세요.')
        self.token_edit = self._line(str(cfg.get('telegram_bot_token', '')), '123456789:AA...', secret=True)
        self.chat_edit = self._line(str(cfg.get('telegram_chat_id', '')), '예: 123456789 또는 -1001234567890')
        content.addWidget(self._field('Bot Token', self.token_edit))
        token_row = QHBoxLayout()
        token_row.addStretch()
        self.token_visibility = SoftCheckBox('토큰 표시')
        self.token_visibility.toggled.connect(self._toggle_token_visibility)
        token_row.addWidget(self.token_visibility)
        content.addLayout(token_row)
        content.addWidget(self._field('Chat ID', self.chat_edit))
        actions = QHBoxLayout()
        actions.addWidget(self._button('발급 방법 보기 ↗', 'secondary', self.open_botfather))
        actions.addWidget(self._button('테스트 알림 전송', 'primary', self.send_test_alerts))
        actions.addStretch()
        content.addLayout(actions)
        layout.addWidget(card)
        note, nlayout = self._card('보안 안내')
        hint = self._label('Bot Token은 config.json에 일반 텍스트로 저장됩니다. 파일을 외부에 공유하지 마시고, 계정이 노출되었다면 BotFather에서 토큰을 재발급하세요.', 'muted')
        hint.setWordWrap(True)
        nlayout.addWidget(hint)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _toggle_token_visibility(self, checked: bool) -> None:
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)

    def _navigate(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.page_title.setText(self.PAGES[index][1])
        self.page_subtitle.setText(self.PAGES[index][2])
        for n, button in enumerate(self.nav_buttons):
            button.setChecked(n == index)
        if index == 3:
            self.db_viewer.load_database()

    def _config_from_form(self) -> dict[str, Any] | None:
        board_url = self.board_edit.text().strip()
        if not board_url:
            QMessageBox.warning(self, '설정 확인', '모니터링 게시판 주소를 입력해 주세요.')
            self._navigate(0)
            self.board_edit.setFocus()
            return None
        # Preserve unknown keys from older/newer config files while updating
        # all v80 form-supported settings, as well as the backend defaults.
        config = dict(DEFAULT_CONFIG)
        config.update(self.loaded_config)
        config.update({
            'board_url': board_url,
            'topic_interval_minutes': self.topic_spin.value(),
            'topic_alert_enabled': self.topic_check.isChecked(),
            'topic_batch_count': self.topic_count.value(),
            'keyword_interval_minutes': self.keyword_spin.value(),
            'new_title_interval_minutes': self.new_title_spin.value(),
            'new_title_alert_enabled': self.new_title_check.isChecked(),
            'new_title_batch_count': self.new_title_count.value(),
            'newbie_title_alert_enabled': self.newbie_title_check.isChecked(),
            'newbie_title_batch_count': self.newbie_title_count.value(),
            'concept_alert_enabled': self.concept_check.isChecked(),
            'concept_batch_count': self.concept_count.value(),
            'title_keywords': [t.strip() for t in self.title_keywords_edit.text().split(',') if t.strip()],
            'body_keywords': [t.strip() for t in self.body_keywords_edit.text().split(',') if t.strip()],
            'comment_keywords': [t.strip() for t in self.comment_keywords_edit.text().split(',') if t.strip()],
            'include_embedded_links': self.link_check.isChecked(),
            'newbie_alert_enabled': self.newbie_check.isChecked(),
            'comment_batch_alert_enabled': self.comment_check.isChecked(),
            'comment_batch_count': self.comment_count.value(),
            'profanity_alert_enabled': self.profanity_check.isChecked(),
            'telegram_bot_token': self.token_edit.text().strip(),
            'telegram_chat_id': self.chat_edit.text().strip(),
            'recent_posts_to_scan': self.posts_spin.value(),
            'request_gap_seconds': self.request_gap_spin.value(),
        })
        return config

    def save_config(self, show_notice: bool = True) -> dict[str, Any] | None:
        config = self._config_from_form()
        if config is None:
            return None
        try:
            CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            QMessageBox.critical(self, '설정 저장 실패', f'설정 파일을 저장하지 못했습니다.\n{exc}')
            return None
        self.loaded_config = config
        self.logger.info('설정 저장 완료: %s', CONFIG_PATH)
        if show_notice:
            QMessageBox.information(self, '설정 저장', f'설정을 저장했습니다.\n{CONFIG_PATH}')
        return config

    @staticmethod
    def _test_alert_messages() -> list[str]:
        """Representative v80 Telegram messages for each alert type."""
        base = 'https://gall.dcinside.com/mgallery/board/view/?id=retrogame&no='
        sep = '\n\n\n'
        posts = [
            ('남씨', '‼️한글패치‼️ 적용 게임 추천', '70001'),
            ('레갤러(123.45.67.89)', '추억의 게임 이야기', '70002'),
            ('게임맨', '레트로 게임기 질문', '70003'),
            ('ㅇㅇ(abc123)', '고전 게임 공략', '70004'),
        ]
        messages = ['📊 1페이지 요약/평화로운 상태']
        messages.append(sep.join(f'🆕 {a} : {t}\n\n본문 : 새 글 본문 요약입니다.\n{base}{70010 + i}' for i, (a, t, _id) in enumerate(posts)))
        messages.append(sep.join(f'👍 {a} : {t}\n\n본문 : 개념글 본문 요약입니다.\n{base}{70020 + i}' for i, (a, t, _id) in enumerate(posts)))
        messages.append(sep.join(f'📝 {a} : {t}\n\n본문 : ‼️한글패치‼️ 관련 본문입니다.\n{base}{70030 + i}' for i, (a, t, _id) in enumerate(posts)))
        messages.append(sep.join(f'💬 {a} : ‼️한글패치‼️ 관련 댓글 내용입니다.\n\n📝 {author} : {title}\n{base}{70040 + i}' for i, ((author, title, _), (a, _, __)) in enumerate(zip(posts, posts))))
        messages.append(sep.join(f'🔗 {a} : {t}\n\n본문 : 링크 포함 본문 요약입니다.\n{base}{70050 + i}\n\n첨부 링크:\nhttps://example.com/post{i}' for i, (a, t, _id) in enumerate(posts)))
        messages.append(sep.join(f'🔗 💬 {a} : 댓글 전체 내용입니다. https://example.com/comment{i}\n\n📝 {author} : {title}\n{base}{70060 + i}\n\n첨부 링크:\nhttps://example.com/comment{i}' for i, ((author, title, _), (a, _, __)) in enumerate(zip(posts, posts))))
        messages.append('💬 수집 댓글 50개' + sep + sep.join(f'📝 {a} : {t}\n{base}{70070 + i}\n\n💬 {a} : 수집된 댓글 내용입니다.' for i, (a, t, _id) in enumerate(posts)))
        profanity = ['ㅆ1발 이건 어렵네요.', '쒸발 진짜 어렵다.', '병 쉰 같은 오류네요.', '개-새끼 표현 감지 예시입니다.']
        messages.append(sep.join(f'🤬 {a} : {profanity[i]}\n\n📝 {author} : {title}\n{base}{70080 + i}' for i, ((author, title, _), (a, _, __)) in enumerate(zip(posts, posts))))
        return messages

    @staticmethod
    def open_botfather() -> None:
        webbrowser.open('https://hdblog.tistory.com/entry/%ED%85%94%EB%A0%88%EA%B7%B8%EB%9E%A8-%EB%B4%87-%ED%86%A0%ED%81%B0%EA%B3%BC-chatid%EB%A5%BC-%EB%B0%9C%EA%B8%89%EB%B0%9B%EB%8A%94-%EB%B0%A9%EB%B2%95')

    def send_test_alerts(self) -> None:
        config = self._config_from_form()
        if config is None:
            return
        if not config['telegram_bot_token'] or not config['telegram_chat_id']:
            QMessageBox.warning(self, '텔레그램 설정', 'Bot Token과 Chat ID를 입력해 주세요.')
            self._navigate(2)
            return
        self.logger.info('테스트 알림 전송 시작: 상황별 목업 %d개', len(self._test_alert_messages()))
        threading.Thread(target=self._send_test_alerts_worker, args=(config,), daemon=True, name='test-alerts').start()

    def _send_test_alerts_worker(self, config: dict[str, Any]) -> None:
        monitor = Monitor(config)
        try:
            for index, message in enumerate(self._test_alert_messages(), 1):
                monitor.send_telegram(message)
                self.logger.info('테스트 알림 전송 완료: %d/%d', index, len(self._test_alert_messages()))
        except Exception:
            self.logger.exception('테스트 알림 전송 중 오류')
        finally:
            monitor.close()

    def _update_status(self, state: str) -> None:
        running = state == '실행 중'
        self.status_text.setText('●  ' + state)
        self.status_text.setStyleSheet('color: #08784E;' if running else 'color: #69788E;')
        self.run_metric.setText(state if state in ('실행 중', '종료 대기', '재시작 대기') else '대기 중')

    def start(self) -> None:
        if any(t.is_alive() for t in self.workers):
            if self._stopping:
                # The button is usable immediately after Stop; postpone the
                # actual restart until old workers have exited (no overlap).
                self._restart_pending = True
                self.start_button.setEnabled(False)
                self._update_status('재시작 대기')
                self.logger.info('기존 모니터링이 종료되면 자동으로 다시 시작합니다.')
            return
        config = self.save_config(show_notice=False)
        if config is None:
            return
        if not config['telegram_bot_token'] or not config['telegram_chat_id']:
            QMessageBox.warning(self, '텔레그램 설정', 'Bot Token과 Chat ID를 입력해 주세요.')
            self._navigate(2)
            return
        if not config['title_keywords'] and not config['body_keywords'] and not config['comment_keywords']:
            if QMessageBox.question(self, '키워드 없음', '키워드가 비어 있어 키워드 알림은 꺼집니다.\n1페이지 분석·새 글 모아보기만 시작할까요?', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes) != QMessageBox.StandardButton.Yes:
                return
        self._stopping = False
        self._restart_pending = False
        self.stop_event.clear()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._update_status('실행 중')
        self.logger.info('모니터링 시작 · 주제 %d분 / 키워드 %d분', config['topic_interval_minutes'], config['keyword_interval_minutes'])
        self.workers = [
            threading.Thread(target=self._run_topic_loop, args=(config,), daemon=True, name='topic-monitor'),
            threading.Thread(target=self._run_keyword_loop, args=(config,), daemon=True, name='keyword-monitor'),
            threading.Thread(target=self._run_new_title_loop, args=(config,), daemon=True, name='new-title-monitor'),
        ]
        for worker in self.workers:
            worker.start()

    def _initial_topic_check(self) -> None:
        config = self._config_from_form()
        if config is None:
            return
        if not config['telegram_bot_token'] or not config['telegram_chat_id']:
            self.logger.warning('최초 주제 알림을 보내려면 Telegram Bot Token과 Chat ID를 먼저 입력하고 저장하세요.')
            return
        self.logger.info('앱 최초 실행 알림을 확인합니다. 기존 히스토리를 무시하고 주제와 제목을 보냅니다.')
        threading.Thread(target=self._run_initial_topic_check, args=(config,), daemon=True, name='initial-topic-monitor').start()

    def _run_initial_topic_check(self, config: dict[str, Any]) -> None:
        monitor = Monitor(config)
        try:
            monitor.run_startup_notifications()
        except Exception:
            self.logger.exception('최초 주제 확인 중 오류')
        finally:
            monitor.close()

    def _run_topic_loop(self, config: dict[str, Any]) -> None:
        monitor = None
        try:
            monitor = CancellableMonitor(config, self.stop_event)
            while not self.stop_event.is_set():
                try:
                    monitor.check_topics()
                except Exception:
                    self.logger.exception('주제 확인 중 오류')
                if self.stop_event.wait(int(config['topic_interval_minutes']) * 60):
                    break
        except MonitoringCancelled:
            pass
        except Exception:
            self.logger.exception('주제 모니터링 스레드 오류')
        finally:
            if monitor is not None:
                monitor.close()

    def _run_keyword_loop(self, config: dict[str, Any]) -> None:
        monitor = None
        try:
            monitor = CancellableMonitor(config, self.stop_event)
            while not self.stop_event.is_set():
                try:
                    monitor.scan_recent()
                except Exception:
                    self.logger.exception('키워드 확인 중 오류')
                if self.stop_event.wait(int(config['keyword_interval_minutes']) * 60):
                    break
        except MonitoringCancelled:
            pass
        except Exception:
            self.logger.exception('키워드 모니터링 스레드 오류')
        finally:
            if monitor is not None:
                monitor.close()

    def _run_new_title_loop(self, config: dict[str, Any]) -> None:
        monitor = None
        try:
            monitor = CancellableMonitor(config, self.stop_event)
            while not self.stop_event.is_set():
                try:
                    monitor.scan_new_titles()
                except Exception:
                    self.logger.exception('새 글 제목 확인 중 오류')
                if config.get('concept_alert_enabled', True):
                    try:
                        monitor.scan_concept_titles()
                    except Exception:
                        self.logger.exception('개념글 확인 중 오류')
                if self.stop_event.wait(int(config['new_title_interval_minutes']) * 60):
                    break
        except MonitoringCancelled:
            pass
        except Exception:
            self.logger.exception('새 글 모니터링 스레드 오류')
        finally:
            if monitor is not None:
                monitor.close()

    def stop(self) -> None:
        if self._stopping or not self.stop_button.isEnabled():
            return
        self._stopping = True
        self._restart_pending = False
        self.stop_event.set()
        # Re-enable Start straight away. If a worker is still finishing an
        # in-flight request, clicking Start queues a safe restart instead.
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._update_status('종료 대기')
        self.logger.info('모니터링 중지 요청 · 진행 중인 요청을 마치면 종료됩니다 (시작 버튼으로 재시작 예약 가능)')

    def _clear_logs(self) -> None:
        self.log_view.clear()

    def _drain_messages(self) -> None:
        buffer: list[str] = []
        try:
            for _ in range(200):
                buffer.append(self.messages.get_nowait())
        except queue.Empty:
            pass
        if buffer:
            self.log_view.appendPlainText('\n'.join(buffer))
        workers_alive = any(t.is_alive() for t in self.workers)
        if self._stopping and not workers_alive:
            self._stopping = False
            self.start_button.setEnabled(True)
            self._update_status('중지됨')
            self.logger.info('모니터링이 완전히 종료되었습니다.')
            if self._restart_pending:
                self._restart_pending = False
                self.start()
        elif (not self._stopping and not self.stop_event.is_set()
              and self.workers and not workers_alive):
            # Recover controls even if every monitoring worker terminated
            # unexpectedly (for example due to an initialization failure).
            self.stop_event.set()
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self._update_status('중지됨')
            self.logger.warning('모니터링 스레드가 모두 종료되어 시작 버튼을 복구했습니다.')

    def _show_first_run_documents(self) -> None:
        if DB_PATH.exists():
            return
        self.logger.info('최초 실행: DB 파일이 없어 readme.txt와 update.txt를 표시합니다.')
        for name in ('readme.txt', 'update.txt'):
            path = BASE_DIR / name
            if not path.exists():
                self.logger.warning('%s 파일을 찾을 수 없습니다: %s', name, path)
                continue
            self.logger.info('[%s]', name)
            try:
                for line in path.read_text(encoding='utf-8').splitlines():
                    if line.strip():
                        self.logger.info('%s', line)
                self.logger.info('%s 표시 완료', name)
            except OSError as exc:
                self.logger.warning('%s를 읽지 못했습니다: %s', name, exc)

    def open_database_viewer(self) -> None:
        self._navigate(3)

    def open_document(self, name: str) -> None:
        path = BASE_DIR / name
        if not path.exists():
            QMessageBox.warning(self, '문서 없음', f'{name} 파일을 찾을 수 없습니다.\n{path}')
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, '문서 열기 실패', f'{name} 파일을 열 수 없습니다.\n{path}')

    def _setup_system_tray(self) -> None:
        """Use the existing penguin icon and keep monitoring on title-bar close."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            # Fall back to normal window closing if the OS has no tray.
            return
        tray = QSystemTrayIcon(load_app_icon(), self)
        tray.setToolTip('갤러리 모니터 v80')
        menu = QMenu(self)
        open_action = QAction('창 열기', self)
        open_action.triggered.connect(self._restore_from_tray)
        exit_action = QAction('프로그램 종료', self)
        exit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(open_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray_icon = tray
        # Closing the only window should not quit the background monitor.
        QApplication.instance().setQuitOnLastWindowClosed(False)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self._allow_close = True
        self.close()
        QApplication.instance().quit()

    def closeEvent(self, event) -> None:
        if not self._allow_close and self.tray_icon is not None and self.tray_icon.isVisible():
            event.ignore()
            self.hide()
            return
        # Preserve the existing shutdown behavior for explicit Exit/fallback.
        self.stop_event.set()
        self.log_timer.stop()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        for handler in tuple(self.logger.handlers):
            if isinstance(handler, QueueLogHandler):
                self.logger.removeHandler(handler)
                handler.close()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName('갤러리 모니터')
    app.setWindowIcon(load_app_icon())
    app.setStyle('Fusion')
    app.setFont(QFont('Malgun Gothic', 9))
    app.setStyleSheet(STYLE)
    window = MonitorApp()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
