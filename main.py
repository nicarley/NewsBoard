"""
NewsBoard + VLC Merge
PyQt6 application that keeps NewsBoard features and uses VLC for video tiles.

Requirements
pip install PyQt6 python-vlc yt-dlp
Install VLC on the system so libvlc is on PATH. On macOS use Homebrew. On Linux install the vlc package.

Files
settings/news_feeds.json
settings/app_state.json
"""

import sys
import os
import re
import json
import math
from pathlib import Path
from urllib.parse import urlparse, parse_qsl
from collections import deque
from typing import Optional

from PyQt6.QtCore import Qt, QUrl, QByteArray, QTimer, QSize, QEvent
from PyQt6.QtGui import QKeySequence, QShortcut, QAction
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QWidget,
    QListWidget,
    QDockWidget,
    QVBoxLayout,
    QTabWidget,
    QListWidgetItem,
    QDialog,
    QFormLayout,
    QDialogButtonBox,
    QStyle,
    QMenu,
    QAbstractItemView,
    QSizePolicy,
    QLabel,
    QToolBar,
    QWidgetAction,
    QFrame,
    QMessageBox,
)

# VLC playback
import vlc

# YouTube resolver
try:
    from yt_dlp import YoutubeDL

    YT_AVAILABLE = True
except Exception:
    YT_AVAILABLE = False

SETTINGS_DIR = Path("settings")
NEWS_FEEDS_FILE = SETTINGS_DIR / "news_feeds.json"
APP_STATE_FILE = SETTINGS_DIR / "app_state.json"

YOUTUBE_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube.com",
    "www.youtube-nocookie.com",
)


# ------------- URL helpers -------------


def _is_youtube_url(u: str) -> bool:
    try:
        netloc = urlparse(u).netloc.lower()
    except ValueError:
        return False
    return any(h == netloc or netloc.endswith("." + h) for h in YOUTUBE_HOSTS)


def build_embed_or_watch(url: str) -> str:
    """Normalize various YouTube forms to a canonical watch URL or return the original URL."""
    try:
        p = urlparse(url)
    except Exception:
        return url
    netloc = p.netloc.lower()
    path = p.path.rstrip("/")

    if "youtube.com" in netloc:
        m = re.match(r"^/channel/([A-Za-z0-9_-]+)/live$", path)
        if m:
            ch = m.group(1)
            return f"https://www.youtube.com/channel/{ch}"

    m = re.search(
        r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|v/|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})",
        url,
    )
    if m:
        vid = m.group(1)
        return f"https://www.youtube.com/watch?v={vid}"

    if "youtube.com" in netloc and path == "/watch":
        qs = dict(parse_qsl(p.query))
        v = qs.get("v", "")
        if len(v) == 11:
            return f"https://www.youtube.com/watch?v={v}"

    return url


def resolve_youtube_to_direct(url: str) -> Optional[str]:
    """Resolve a YouTube URL to a direct media URL that VLC can play."""
    if not YT_AVAILABLE:
        return None
    ydl_opts = {
        "quiet": True,
        "noprogress": True,
        "format": "best[protocol*=http][ext=mp4]/best[protocol*=http]/best/bestvideo+bestaudio/best",
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "url" in info:
                return info["url"]
            fmts = info.get("formats", [])
            for f in reversed(fmts):
                u = f.get("url")
                if not u:
                    continue
                proto = f.get("protocol", "")
                if proto.startswith("http") or u.endswith(".m3u8"):
                    return u
    except Exception:
        return None
    return None


# ------------- VLC tile -------------


class VlcTile(QFrame):
    def __init__(self, url: str, title: str, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.url = url
        self.title = title or "Video"

        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()

        self.canvas = QWidget(self)
        self.canvas.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.canvas.setMinimumSize(160, 90)

        self.label = QLabel(self.title, self)
        self.label.setStyleSheet(
            "color: white; background: rgba(0,0,0,.45); padding: 2px 6px; border-radius: 4px;"
        )

        style = self.style()
        self.mute_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaVolumeMuted), "", self
        )
        self.mute_button.setToolTip("Mute or unmute audio")
        self.mute_button.setCheckable(True)
        self.mute_button.setFixedSize(24, 24)

        self.remove_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), "", self
        )
        self.remove_button.setToolTip("Remove video")
        self.remove_button.setFixedSize(24, 24)

        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.canvas, 0, 0)

        self.is_muted = True
        self.player.audio_set_mute(True)
        self.mute_button.setChecked(True)

        QTimer.singleShot(0, self._place_overlay)

        self.play_url(self.url)
        self.mute_button.clicked.connect(self._toggle_mute)

    def event(self, e: QEvent):
        if e.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.PolishRequest,
        ):
            QTimer.singleShot(0, self._place_overlay)
        return super().event(e)

    def _place_overlay(self):
        margin = 6
        w = self.width()
        self.label.move(margin, margin)
        self.remove_button.move(max(0, w - self.remove_button.width() - margin), margin)
        self.mute_button.move(
            max(
                0,
                w
                - self.remove_button.width()
                - self.mute_button.width()
                - margin
                - 4,
            ),
            margin,
        )

    def _widget_id(self) -> int:
        return int(self.canvas.winId())

    def _attach_output(self):
        wid = self._widget_id()
        if sys.platform == "win32":
            self.player.set_hwnd(wid)
        elif sys.platform == "darwin":
            self.player.set_nsobject(wid)
        else:
            self.player.set_xwindow(wid)

    def play_url(self, url: str):
        src = build_embed_or_watch(url)
        if _is_youtube_url(src):
            direct = resolve_youtube_to_direct(src)
            if direct:
                src = direct
        media = self.instance.media_new(src)
        self.player.set_media(media)
        self._attach_output()
        self.player.play()

    def _toggle_mute(self):
        self.is_muted = not self.is_muted
        self.player.audio_set_mute(self.is_muted)
        style = self.style()
        icon = style.standardIcon(
            QStyle.StandardPixmap.SP_MediaVolumeMuted
            if self.is_muted
            else QStyle.StandardPixmap.SP_MediaVolume
        )
        self.mute_button.setIcon(icon)

    def stop(self):
        try:
            self.player.stop()
        except Exception:
            pass

    def closeEvent(self, e):
        self.stop()
        super().closeEvent(e)


# ------------- Feed dialog and list dock -------------


class FeedDialog(QDialog):
    def __init__(self, parent=None, name="", url=""):
        super().__init__(parent)
        self.setWindowTitle("Add or Edit Feed")
        form = QFormLayout(self)
        self.name_input = QLineEdit(name)
        self.url_input = QLineEdit(url)
        self.name_label = QLabel("Name:")
        form.addRow(self.name_label, self.name_input)
        form.addRow("URL:", self.url_input)
        box = QDialogButtonBox()
        box.setStandardButtons(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addWidget(box)

    def set_name_label(self, text: str):
        self.name_label.setText(text)

    def get_feed_data(self):
        return self.name_input.text(), self.url_input.text()


class ListManager(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Manage Lists", parent)
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        self.tabs = QTabWidget()
        self.setWidget(self.tabs)

        style = self.style()

        self.news_feed_tab = QWidget()
        self.news_feed_layout = QVBoxLayout(self.news_feed_tab)
        self.news_feed_layout.setContentsMargins(6, 6, 6, 6)
        self.news_feed_layout.setSpacing(6)

        self.toolbar = QWidget()
        self.toolbar_layout = QHBoxLayout(self.toolbar)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout.setSpacing(6)

        self.add_feed_to_grid_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton), " Add Selected"
        )
        self.add_all_feeds_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward), " Add All"
        )
        self.add_new_feed_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_FileIcon), " New"
        )
        self.edit_feed_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_DesktopIcon), " Edit"
        )
        self.remove_feed_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), " Remove"
        )
        self.remove_all_feeds_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_BrowserStop), " Clear All"
        )

        self.toolbar_layout.addWidget(self.add_feed_to_grid_button)
        self.toolbar_layout.addWidget(self.add_all_feeds_button)
        self.toolbar_layout.addSpacing(8)
        self.toolbar_layout.addWidget(self.add_new_feed_button)
        self.toolbar_layout.addWidget(self.edit_feed_button)
        self.toolbar_layout.addWidget(self.remove_feed_button)
        self.toolbar_layout.addStretch()
        self.toolbar_layout.addWidget(self.remove_all_feeds_button)

        self.news_feed_list_widget = QListWidget()
        self.news_feed_list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.news_feed_list_widget.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove
        )
        self.news_feed_list_widget.setDragEnabled(True)
        self.news_feed_list_widget.setAcceptDrops(True)
        self.news_feed_list_widget.setDropIndicatorShown(True)
        self.news_feed_list_widget.setSizePolicy(
            self.news_feed_list_widget.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Expanding,
        )

        self.news_feed_layout.addWidget(self.toolbar, 0)
        self.news_feed_layout.addWidget(self.news_feed_list_widget, 1)
        self.tabs.addTab(self.news_feed_tab, "News Feeds")

        self.grid_tab = QWidget()
        self.grid_layout = QVBoxLayout(self.grid_tab)
        self.grid_layout.setContentsMargins(6, 6, 6, 6)
        self.grid_layout.setSpacing(6)

        self.grid_toolbar = QWidget()
        self.grid_toolbar_layout = QHBoxLayout(self.grid_toolbar)
        self.grid_toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_toolbar_layout.setSpacing(6)

        self.remove_from_grid_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), " Remove"
        )
        self.grid_toolbar_layout.addWidget(self.remove_from_grid_button)
        self.grid_toolbar_layout.addStretch()

        self.grid_list_widget = QListWidget()
        self.grid_list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )

        self.grid_layout.addWidget(self.grid_toolbar, 0)
        self.grid_layout.addWidget(self.grid_list_widget, 1)
        self.tabs.addTab(self.grid_tab, "Grid")


# ------------- Main window -------------


class NewsBoardVLC(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("News Board")
        self.setBaseSize(1280, 720)

        self.central_widget = QWidget()
        the_layout = QGridLayout(self.central_widget)
        the_layout.setContentsMargins(0, 0, 0, 0)
        the_layout.setSpacing(4)
        self.grid_layout = the_layout
        self.setCentralWidget(self.central_widget)

        self.video_widgets: list[VlcTile] = []

        self.list_manager = ListManager(self)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.list_manager)

        self.list_manager.remove_from_grid_button.clicked.connect(
            self.remove_video_from_grid_list
        )
        self.list_manager.add_feed_to_grid_button.clicked.connect(
            self.add_video_from_feed
        )
        self.list_manager.add_all_feeds_button.clicked.connect(self.add_all_feeds)
        self.list_manager.add_new_feed_button.clicked.connect(self.add_new_feed)
        self.list_manager.edit_feed_button.clicked.connect(self.edit_feed)
        self.list_manager.remove_feed_button.clicked.connect(self.remove_feed)
        self.list_manager.remove_all_feeds_button.clicked.connect(self.remove_all_feeds)
        self.list_manager.news_feed_list_widget.model().rowsMoved.connect(
            self.reorder_news_feeds
        )

        self.shortcut_delete = QShortcut(QKeySequence("Delete"), self)
        self.shortcut_delete.activated.connect(self.remove_video_from_grid_list)

        self.news_feeds: dict[str, str] = {}
        self.load_news_feeds()
        self.list_manager.tabs.setCurrentIndex(0)

        self._build_top_toolbar()
        self._build_menu_bar()

        self._queue = deque()
        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(0)
        self._queue_timer.timeout.connect(self._process_queue)

        self.load_state()

    def remove_video_from_grid_list(self):
        items = self.list_manager.grid_list_widget.selectedItems()
        if not items:
            return
        for item in items:
            w = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(w, VlcTile):
                self.remove_video_widget(w)

    def _build_menu_bar(self):
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")
        file_menu.addAction("Add Video from URL...", self.add_video_from_input_dialog)
        file_menu.addSeparator()
        file_menu.addAction("Exit", self.close)

        view_menu = menu_bar.addMenu("&View")
        toggle_action = QAction("Toggle List Manager", self)
        toggle_action.setCheckable(True)
        toggle_action.setChecked(self.list_manager.isVisible())
        toggle_action.toggled.connect(self.list_manager.setVisible)
        self.list_manager.visibilityChanged.connect(toggle_action.setChecked)
        self.manage_lists_button.toggled.connect(self.list_manager.setVisible)
        self.list_manager.visibilityChanged.connect(
            self.manage_lists_button.setChecked
        )
        view_menu.addAction(toggle_action)

        playback_menu = menu_bar.addMenu("&Playback")
        playback_menu.addAction("Mute All", self.mute_all)
        playback_menu.addAction("Unmute All", self.unmute_all)
        playback_menu.addSeparator()
        playback_menu.addAction("Remove All Videos", self.remove_all_videos)

    def _build_top_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setFloatable(False)
        sz = self.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
        if sz <= 0:
            sz = 16
        tb.setIconSize(QSize(sz, sz))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)

        self.manage_lists_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ToolBarHorizontalExtensionButton),
            " Manage Lists",
        )
        self.manage_lists_button.setCheckable(True)
        self.manage_lists_button.setChecked(True)
        self.manage_lists_button.setToolTip("Show or hide the list manager panel")

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "Enter YouTube or direct stream URL or paste an iframe tag"
        )
        self.url_input.setToolTip("Enter a URL or iframe code to add a video to the grid")

        self.add_video_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton),
            " Add Video",
        )
        self.add_video_button.clicked.connect(self.add_video_from_input)

        tb.addWidget(self.manage_lists_button)
        spacer_small = QWidget()
        spacer_small.setFixedWidth(6)
        tb.addWidget(spacer_small)
        url_action = QWidgetAction(self)
        url_action.setDefaultWidget(self.url_input)
        tb.addAction(url_action)
        tb.addWidget(self.add_video_button)

        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        tb.addWidget(spacer)
        self.mute_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolumeMuted),
            " Mute All",
        )
        self.mute_all_button.clicked.connect(self.mute_all)
        tb.addWidget(self.mute_all_button)
        self.remove_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon), " Remove All"
        )
        self.remove_all_button.clicked.connect(self.remove_all_videos)
        tb.addWidget(self.remove_all_button)

        self.shortcut_mute_all = QShortcut(QKeySequence("Ctrl+M"), self)
        self.shortcut_mute_all.activated.connect(self.mute_all)

        self.shortcut_unmute_all = QShortcut(QKeySequence("Ctrl+Shift+M"), self)
        self.shortcut_unmute_all.activated.connect(self.unmute_all)

        self.shortcut_add_all_feeds = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
        self.shortcut_add_all_feeds.activated.connect(self.add_all_feeds)

        self.shortcut_delete = QShortcut(QKeySequence("Delete"), self)
        self.shortcut_delete.activated.connect(self.remove_video_from_grid_list)

    # ------------- State -------------
    def save_state(self):
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "geometry": self.saveGeometry().toBase64().data().decode(),
            "videos": [(w.url, w.title) for w in self.video_widgets],
        }
        with APP_STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)

    def load_state(self):
        if not APP_STATE_FILE.exists():
            return
        try:
            with APP_STATE_FILE.open("r", encoding="utf-8") as f:
                state = json.load(f)
            self.restoreGeometry(QByteArray.fromBase64(state["geometry"].encode()))
            for url, title in state.get("videos", []):
                self._enqueue(url, title)
        except Exception:
            pass

    def closeEvent(self, event):
        self.save_state()
        super().closeEvent(event)

    # ------------- Feeds -------------
    def load_news_feeds(self):
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with NEWS_FEEDS_FILE.open("r", encoding="utf-8") as f:
                self.news_feeds = json.load(f)
        except Exception:
            default_feeds = {
                "Associated Press": "https://www.youtube.com/channel/UC52X5wxOL_s5ywGvmcU7v8g",
                "Reuters": "https://www.youtube.com/channel/UChqUTb7kYRx8-EiaN3XFrSQ",
                "CNN": "https://www.youtube.com/watch?v=live",
            }
            self.news_feeds = default_feeds
            self.save_news_feeds()

        self.list_manager.news_feed_list_widget.clear()
        for name, url in self.news_feeds.items():
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, url)
            self.list_manager.news_feed_list_widget.addItem(item)

    def save_news_feeds(self):
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        with NEWS_FEEDS_FILE.open("w", encoding="utf-8") as f:
            json.dump(self.news_feeds, f, indent=4)

    def add_video_from_input_dialog(self):
        dialog = FeedDialog(self, name="", url="")
        dialog.setWindowTitle("Add Video from URL")
        dialog.set_name_label("Title optional:")
        if dialog.exec():
            name, url = dialog.get_feed_data()
            if url:
                self._enqueue(url, name or "Pasted Video")

    def add_video_from_input(self):
        url_or_iframe = self.url_input.text().strip()
        if not url_or_iframe:
            return
        m = re.search(r'<iframe.*?src="([^"]*)"', url_or_iframe, re.IGNORECASE)
        url = m.group(1) if m else url_or_iframe
        self._enqueue(url, "Pasted Video")
        self.url_input.clear()

    def add_video_from_feed(self):
        selected = self.list_manager.news_feed_list_widget.selectedItems()
        if not selected:
            return
        for item in selected:
            feed_name = item.text()
            feed_url = item.data(Qt.ItemDataRole.UserRole)
            self._enqueue(feed_url, feed_name)

    def add_all_feeds(self):
        for name, url in self.news_feeds.items():
            self._enqueue(url, name)

    def add_new_feed(self):
        dialog = FeedDialog(self)
        if dialog.exec():
            name, url = dialog.get_feed_data()
            if name and url:
                self.news_feeds[name] = url
                self.save_news_feeds()
                self.load_news_feeds()

    def edit_feed(self):
        selected = self.list_manager.news_feed_list_widget.selectedItems()
        if not selected:
            return
        item = selected[0]
        old_name = item.text()
        old_url = item.data(Qt.ItemDataRole.UserRole)
        dialog = FeedDialog(self, name=old_name, url=old_url)
        if dialog.exec():
            new_name, new_url = dialog.get_feed_data()
            if new_name and new_url:
                self.news_feeds.pop(old_name, None)
                self.news_feeds[new_name] = new_url
                self.save_news_feeds()
                self.load_news_feeds()

    def remove_feed(self):
        selected = self.list_manager.news_feed_list_widget.selectedItems()
        if not selected:
            return
        for item in selected:
            name = item.text()
            self.news_feeds.pop(name, None)
        self.save_news_feeds()
        self.load_news_feeds()

    def remove_all_feeds(self):
        if not self.news_feeds:
            return
        self.news_feeds.clear()
        self.save_news_feeds()
        self.load_news_feeds()

    def reorder_news_feeds(self, *_):
        new_order_names = []
        for i in range(self.list_manager.news_feed_list_widget.count()):
            new_order_names.append(self.list_manager.news_feed_list_widget.item(i).text())
        new_news_feeds = {}
        for name in new_order_names:
            if name in self.news_feeds:
                new_news_feeds[name] = self.news_feeds[name]
        self.news_feeds = new_news_feeds
        self.save_news_feeds()

    # ------------- Queue and grid -------------
    def _enqueue(self, url, title):
        self._queue.append((url, title))
        if not self._queue_timer.isActive():
            self._queue_timer.start()

    def _process_queue(self):
        if not self._queue:
            self._queue_timer.stop()
            return
        url, title = self._queue.popleft()
        self.create_video_widget(url, title)

    def create_video_widget(self, url, title):
        vw = VlcTile(url, title, parent=self)
        vw.remove_button.clicked.connect(lambda: self.remove_video_widget(vw))
        self.video_widgets.append(vw)
        self.update_grid()

    def remove_video_widget(self, widget):
        if widget in self.video_widgets:
            self.video_widgets.remove(widget)
            try:
                widget.deleteLater()
            except Exception:
                pass
            self.update_grid()

    def remove_all_videos(self):
        for w in list(self.video_widgets):
            self.remove_video_widget(w)

    def update_grid(self):
        widgets_in_layout = set()
        for i in range(self.grid_layout.count()):
            item = self.grid_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), VlcTile):
                widgets_in_layout.add(item.widget())

        video_widgets_set = set(self.video_widgets)
        widgets_to_remove = widgets_in_layout - video_widgets_set
        for w in widgets_to_remove:
            self.grid_layout.removeWidget(w)

        n = len(self.video_widgets)
        if n == 0:
            self.list_manager.grid_list_widget.clear()
            return
        cols = max(1, math.isqrt(n))
        if cols * cols < n:
            cols += 1
        for i, w in enumerate(self.video_widgets):
            row = i // cols
            col = i % cols
            self.grid_layout.addWidget(w, row, col)

        self.list_manager.grid_list_widget.clear()
        for w in self.video_widgets:
            item = QListWidgetItem(w.title)
            item.setData(Qt.ItemDataRole.UserRole, w)
            self.list_manager.grid_list_widget.addItem(item)

    # ------------- Playback helpers -------------
    def mute_all(self):
        for vw in list(self.video_widgets):
            try:
                if not vw.is_muted:
                    vw._toggle_mute()
            except Exception:
                pass

    def unmute_all(self):
        for vw in list(self.video_widgets):
            try:
                if vw.is_muted:
                    vw._toggle_mute()
            except Exception:
                pass


# ------------- Entry point -------------

def main():
    app = QApplication(sys.argv)
    win = NewsBoardVLC()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
