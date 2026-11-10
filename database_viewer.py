"""PySide6 viewer for Gallery Monitor's existing v80 SQLite database."""
from __future__ import annotations

import re
import sqlite3
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from app_icon import load_app_icon
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTabWidget, QTableView, QVBoxLayout, QWidget,
    QAbstractItemView, QHeaderView,
)

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent

VIEW_STYLE = """
QWidget { font-family: 'Malgun Gothic', 'Segoe UI', sans-serif; font-size: 9pt; color: #22354E; }
QWidget#dbRoot { background: #F3F6FC; }
QFrame#dbCard { background: white; border: 1px solid #E4EAF2; border-radius: 13px; }
QLabel#dbTitle { font-size: 14pt; font-weight: 800; color: #182D49; }
QLabel#dbHint { color: #73829A; font-size: 9pt; }
QLineEdit { border: 1px solid #DCE5F0; border-radius: 8px; padding: 7px 9px; background: #F9FBFE; }
QLineEdit:focus { border: 1px solid #4285EE; }
QPushButton { background: white; color: #314664; border: 1px solid #DBE4F0; border-radius: 8px; padding: 7px 11px; font-weight: 700; }
QPushButton:hover { background: #EFF5FF; }
QPushButton#reload { color: white; background: #2C75E8; border-color: #2C75E8; }
/* Remove the native tab shadow/top edge and keep the page flush with the tabs. */
QTabWidget::pane { border: 0; background: #FFFFFF; }
QTabBar { background: transparent; border: 0; }
QTabBar::tab {
    background: #E9EEF6; color: #65758F; padding: 8px 11px;
    margin-right: 3px; border: 0;
    border-top-left-radius: 7px; border-top-right-radius: 7px;
}
QTabBar::tab:selected { color: #1A56A3; background: #FFFFFF; font-weight: 700; }
QTabBar::tab:hover:!selected { background: #DBE5F6; color: #435A81; }
QTableView { background: white; alternate-background-color: #F7F9FD; gridline-color: #EDF1F6; selection-background-color: #DDEBFE; selection-color: #16365C; border: 0; }
QHeaderView::section { background: #F4F7FC; border: none; border-bottom: 1px solid #E0E7F0; border-right: 1px solid #E9EFF6; color: #435772; font-weight: bold; padding: 7px; }
"""


class RecordModel(QAbstractTableModel):
    """Virtual table model: avoids creating one QWidget per SQLite record."""

    def __init__(self, headers: tuple[str, ...], parent: QWidget | None = None):
        super().__init__(parent)
        self.headers = headers
        self.rows: list[tuple[Any, ...]] = []

    def set_rows(self, rows: list[tuple[Any, ...]]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.rows)):
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            value = self.rows[index.row()][index.column()]
            return '' if value is None else str(value)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None


class DatabaseViewer(QWidget):
    TABLES = (
        ('알림 전송 내역', ('id', 'kind', 'sent_at', 'message'), (70, 130, 180, 610), 'SELECT id, kind, sent_at, message FROM notification_history ORDER BY id DESC'),
        ('전송한 새 글 제목', ('post_id', 'title', 'url', 'sent_at'), (95, 380, 420, 170), 'SELECT post_id, title, url, sent_at FROM sent_titles ORDER BY sent_at DESC'),
        ('확인한 항목', ('item_id', 'seen_at'), (520, 190), 'SELECT item_id, seen_at FROM seen ORDER BY seen_at DESC'),
        ('작성자·식별코드', ('author', 'first_seen'), (420, 220), 'SELECT author, first_seen FROM author_history ORDER BY first_seen DESC'),
        ('뉴비 감지 내역', ('id', 'author', 'kind', 'post_title', 'comment', 'post_url', 'detected_at'), (60, 190, 100, 300, 440, 330, 170), 'SELECT id, author, kind, post_title, comment, post_url, detected_at FROM newbie_history ORDER BY id DESC'),
        ('확인한 댓글', ('post_id', 'comment_id', 'post_title', 'author', 'comment', 'post_url', 'checked_at'), (90, 100, 280, 190, 460, 330, 170), 'SELECT post_id, comment_id, post_title, author, comment, post_url, checked_at FROM comment_history ORDER BY checked_at DESC'),
    )

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName('dbRoot')
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(VIEW_STYLE)
        self.path_edit = QLineEdit(str(BASE_DIR / 'monitor_state.sqlite3'))
        self.status_label = QLabel('데이터베이스 파일을 선택하세요.')
        self.tables: list[QTableView] = []
        self.models: list[RecordModel] = []
        self._build_ui()
        QTimer.singleShot(200, self.load_database)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 5, 5, 5)
        outer.setSpacing(10)
        intro = QLabel('저장된 모니터링 내역')
        intro.setObjectName('dbTitle')
        outer.addWidget(intro)
        info = QLabel('알림 전송 기록과 검사 결과를 로컬 SQLite에서 확인합니다.')
        info.setObjectName('dbHint')
        outer.addWidget(info)
        actions = QHBoxLayout()
        actions.addWidget(self.path_edit, 1)
        browse = QPushButton('파일 선택')
        browse.clicked.connect(self.choose_file)
        actions.addWidget(browse)
        reload_button = QPushButton('↻  새로고침')
        reload_button.setObjectName('reload')
        reload_button.clicked.connect(self.load_database)
        actions.addWidget(reload_button)
        outer.addLayout(actions)
        self.status_label.setObjectName('dbHint')
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)
        for name, columns, widths, query in self.TABLES:
            model = RecordModel(columns, self)
            table = QTableView()
            table.setModel(model)
            table.setAlternatingRowColors(True)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(28)
            table.horizontalHeader().setStretchLastSection(True)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            for index, width in enumerate(widths):
                table.setColumnWidth(index, width)
            table.doubleClicked.connect(self.open_selected_link)
            self.models.append(model)
            self.tables.append(table)
            self.tabs.addTab(table, name)
        outer.addWidget(self.tabs, 1)
        bottom = QLabel('행을 더블클릭하면 해당 게시글 링크가 기본 브라우저에서 열립니다 (링크가 있는 기록만).')
        bottom.setObjectName('dbHint')
        outer.addWidget(bottom)

    def choose_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, 'SQLite 데이터베이스 선택', str(BASE_DIR), 'SQLite DB (*.sqlite3 *.db);;모든 파일 (*.*)')
        if selected:
            self.path_edit.setText(selected)
            self.load_database()

    def load_database(self) -> None:
        path = Path(self.path_edit.text().strip())
        if not path.is_file():
            for model in self.models:
                model.set_rows([])
            self.status_label.setText(f'파일을 찾을 수 없습니다: {path}')
            return
        results: list[list[tuple[Any, ...]]] = []
        try:
            # v80 default DB schema may not contain all optional history tables.
            with sqlite3.connect(f'file:{quote(path.resolve().as_posix(), safe="/:")}?mode=ro', uri=True) as conn:
                for index, (_, _, _, query) in enumerate(self.TABLES):
                    try:
                        results.append(conn.execute(query).fetchall())
                    except sqlite3.Error as exc:
                        # The original viewer treats these three history tables
                        # as optional, for compatibility with older databases.
                        if index >= 3 or 'no such table' in str(exc).lower():
                            results.append([])
                        else:
                            raise
        except sqlite3.Error as exc:
            QMessageBox.critical(self, 'DB 열기 실패', str(exc))
            self.status_label.setText('DB를 읽지 못했습니다.')
            return
        for model, rows in zip(self.models, results):
            model.set_rows(rows)
        counts = [len(rows) for rows in results]
        self.status_label.setText(
            f'{path.name}  ·  알림 {counts[0]:,}건  ·  전송 제목 {counts[1]:,}건  ·  '
            f'확인 항목 {counts[2]:,}건  ·  뉴비 {counts[4]:,}건  ·  댓글 {counts[5]:,}건'
        )

    def open_selected_link(self, index: QModelIndex) -> None:
        current = self.tabs.currentIndex()
        model = self.models[current]
        if not index.isValid() or not (0 <= index.row() < len(model.rows)):
            return
        values = model.rows[index.row()]
        if current == 1:
            url = str(values[2]) if len(values) > 2 else ''
        elif current == 0:
            url = next(iter(re.findall(r'https?://[^\s<>]+', str(values[3]) if len(values) > 3 else '')), '')
            url = url.rstrip(',.!)]}"\'')
        elif current == 4:
            url = str(values[5]) if len(values) > 5 else ''
        elif current == 5:
            url = str(values[5]) if len(values) > 5 else ''
        else:
            return
        if url.startswith(('http://', 'https://')):
            webbrowser.open(url)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName('갤러리 모니터 DB 조회기')
    app.setWindowIcon(load_app_icon())
    app.setStyle('Fusion')
    app.setFont(QFont('Malgun Gothic', 9))
    app.setStyleSheet(VIEW_STYLE)
    viewer = DatabaseViewer()
    viewer.setWindowTitle('🐧 갤러리 모니터  |  데이터베이스 조회')
    viewer.resize(1080, 700)
    viewer.setMinimumSize(800, 500)
    viewer.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
