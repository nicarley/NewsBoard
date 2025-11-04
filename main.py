import sys
import re
import json
import math
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qsl
from collections import deque
from typing import Optional

from PyQt6.QtCore import (
    Qt,
    QByteArray,
    QTimer,
    QSize,
    QObject,
    pyqtSignal,
)
from PyQt6.QtGui import QKeySequence, QShortcut, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QWidget,
    QListWidget,
    QDockWidget,
    QTabWidget,
    QListWidgetItem,
    QDialog,
    QFormLayout,
    QDialogButtonBox,
    QStyle,
    QAbstractItemView,
    QSizePolicy,
    QLabel,
    QToolBar,
    QWidgetAction,
    QFrame,
    QSlider,
)

from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

version="2025.11.04"

try:
    from yt_dlp import YoutubeDL
    YT_AVAILABLE = True
except Exception:
    YT_AVAILABLE = False

SETTINGS_DIR = Path("resources/settings")
NEWS_FEEDS_FILE = SETTINGS_DIR / "news_feeds.json"
APP_STATE_FILE = SETTINGS_DIR / "app_state.json"

YOUTUBE_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube.com",
    "www.youtube-nocookie.com",
)

# ---------------- URL helpers ----------------

def _is_youtube_url(u: str) -> bool:
    try:
        netloc = urlparse(u).netloc.lower()
    except ValueError:
        return False
    return any(h == netloc or netloc.endswith("." + h) for h in YOUTUBE_HOSTS)

def build_embed_or_watch(url: str) -> str:
    m = re.search(r'<iframe[^>]+src="([^"]+)"', url, re.IGNORECASE)
    if m:
        url = m.group(1)

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
    if not YT_AVAILABLE:
        return None
    ydl_opts = {
        "quiet": True,
        "noprogress": True,
        "format": (
            "best[acodec!=none][protocol*=http]/"
            "best[acodec!=none]/"
            "best"
        ),
        "nocookie": True,
        "cachedir": False,
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "url" in info and info.get("acodec") not in (None, "none"):
                return info["url"]
            for f in reversed(info.get("formats", [])):
                u = f.get("url")
                if not u:
                    continue
                if f.get("acodec") in (None, "none"):
                    continue
                proto = f.get("protocol", "")
                if u.endswith(".m3u8") or proto.startswith("http"):
                    return u
    except Exception:
        return None
    return None

# ---------------- Async resolver ----------------

class YtResolveWorker(QObject):
    resolved = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = url

    def start(self):
        def run():
            direct = resolve_youtube_to_direct(self.url)
            if direct:
                self.resolved.emit(direct)
            else:
                self.failed.emit("Could not resolve YouTube to a direct stream")
        t = threading.Thread(target=run, daemon=True)
        t.start()

# ---------------- Qt Multimedia tile ----------------

class QtTile(QFrame):
    # Signals so the controller always receives actions regardless of Qt parenting
    requestToggle = pyqtSignal(object)   # emits self when user clicks the mute button
    started = pyqtSignal(object)         # emits self when playback changes or starts

    def __init__(self, url: str, title: str, parent=None, muted: bool = True):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.url = url
        self.title = title or "Video"
        self.is_muted = muted


        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.video_widget = QVideoWidget(self)
        self.video_widget.setMinimumSize(160, 90)
        outer.addWidget(self.video_widget, 1)

        controls = QWidget(self)
        controls.setObjectName("controls")
        controls.setStyleSheet("#controls { background: rgba(0,0,0,0.55); } QLabel { color: white; }")
        row = QHBoxLayout(controls)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(6)

        self.label = QLabel(self.title, controls)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.mute_button = QPushButton("", controls)
        self.mute_button.setToolTip("Mute or unmute audio for this tile")
        self.mute_button.setFixedSize(24, 24)

        self.remove_button = QPushButton("", controls)
        self.remove_button.setToolTip("Remove video")
        self.remove_button.setFixedSize(24, 24)

        row.addWidget(self.label)
        row.addStretch()
        row.addWidget(self.mute_button)
        row.addWidget(self.remove_button)
        outer.addWidget(controls, 0)

        # media setup
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget.videoSink())

        # initial audio state
        self.audio.setMuted(self.is_muted)
        self.audio.setVolume(0.0 if self.is_muted else 0.85)

        # state hooks
        self.player.playbackStateChanged.connect(lambda *_: self.started.emit(self))
        self.player.mediaStatusChanged.connect(lambda *_: self.started.emit(self))

        self._refresh_icons()

        # important: signal to controller rather than calling parent
        self.mute_button.clicked.connect(lambda: self.requestToggle.emit(self))

        # start playback
        self.play_url(self.url)

    # ---------- Playback ----------
    def play_url(self, url: str):
        from PyQt6.QtCore import QUrl
        src = build_embed_or_watch(url)
        if _is_youtube_url(src):
            if YT_AVAILABLE:
                self.label.setText(f"{self.title}  Resolving…")
                self._yt_worker = YtResolveWorker(src, self)
                self._yt_worker.resolved.connect(lambda direct: self._apply_source(QUrl(direct)))
                self._yt_worker.failed.connect(lambda _msg: self.label.setText(f"{self.title}  YouTube resolve failed"))
                self._yt_worker.start()
                return
            else:
                self.label.setText(f"{self.title}  yt_dlp not installed")
        # direct
        self._apply_source(QUrl(src))

    def _apply_source(self, qurl):
        self.player.setSource(qurl)
        self.player.play()
        self.label.setText(self.title)
        QTimer.singleShot(0, lambda: self.started.emit(self))

    # ---------- Audio control ----------
    def hard_mute(self):
        self.audio.setMuted(True)
        self.audio.setVolume(0.0)
        self.is_muted = True
        self._refresh_icons()

    def make_active(self, volume_percent: int):
        vol = max(0, min(100, int(volume_percent))) / 100.0
        self.audio.setMuted(False)
        self.audio.setVolume(vol)
        self.is_muted = False
        self._refresh_icons()

    # API used by controller
    def set_mute_state(self, muted: bool):
        if muted:
            self.hard_mute()
        else:
            self.audio.setMuted(False)
            self.is_muted = False
            self._refresh_icons()

    def set_audio_for_active(self, volume_percent: int):
        self.make_active(volume_percent)

    # ---------- UI ----------
    def _refresh_icons(self):
        style = self.style()
        self.mute_button.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaVolumeMuted if self.is_muted
                               else QStyle.StandardPixmap.SP_MediaVolume)
        )
        self.remove_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon))

    def stop(self):
        try:
            self.player.stop()
        except Exception:
            pass

    def closeEvent(self, e):
        self.stop()
        super().closeEvent(e)

# ---------------- Feed dialog and list dock ----------------

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

# ---------------- Main window with single speaker policy ----------------

class NewsBoardVLC(QMainWindow):
    AUDIO_RETRY_COUNT = 6
    AUDIO_RETRY_DELAY_MS = 150

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'NewsBoard {version}')
        self.setWindowIcon(QIcon("resources/icon.ico"))
        self.setBaseSize(1280, 720)

        self.central_widget = QWidget()
        the_layout = QGridLayout(self.central_widget)
        the_layout.setContentsMargins(0, 0, 0, 0)
        the_layout.setSpacing(4)
        self.grid_layout = the_layout
        self.setCentralWidget(self.central_widget)

        self.video_widgets: list[QtTile] = []
        self.currently_unmuted: Optional[QtTile] = None
        self.active_volume: int = 85
        self.allow_auto_select: bool = True
        self._audio_enforce_generation = 0

        self.list_manager = ListManager(self)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.list_manager)

        self.list_manager.remove_from_grid_button.clicked.connect(self.remove_video_from_grid_list)
        self.list_manager.add_feed_to_grid_button.clicked.connect(self.add_video_from_feed)
        self.list_manager.add_all_feeds_button.clicked.connect(self.add_all_feeds)
        self.list_manager.add_new_feed_button.clicked.connect(self.add_new_feed)
        self.list_manager.edit_feed_button.clicked.connect(self.edit_feed)
        self.list_manager.remove_feed_button.clicked.connect(self.remove_feed)
        self.list_manager.remove_all_feeds_button.clicked.connect(self.remove_all_feeds)
        self.list_manager.news_feed_list_widget.model().rowsMoved.connect(self.reorder_news_feeds)

        self.shortcut_delete = QShortcut(QKeySequence("Delete"), self)
        self.shortcut_delete.activated.connect(self.remove_video_from_grid_list)

        self.news_feeds: dict[str, str] = {}
        self.load_news_feeds()
        self.list_manager.tabs.setCurrentIndex(0)

        self._build_top_toolbar()

        self._queue = deque()
        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(0)
        self._queue_timer.timeout.connect(self._process_queue)

        self.load_state()
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(self.active_volume)
        self.volume_label.setText(f"Volume {self.active_volume}")
        self.volume_slider.blockSignals(False)

    # Actions from grid tab
    def remove_video_from_grid_list(self):
        items = self.list_manager.grid_list_widget.selectedItems()
        if not items:
            return
        for item in items:
            w = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(w, QtTile):
                self.remove_video_widget(w)

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
        self.manage_lists_button.toggled.connect(self.list_manager.setVisible)
        self.list_manager.visibilityChanged.connect(self.manage_lists_button.setChecked)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Enter YouTube or direct stream URL or paste an iframe tag")
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
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.volume_label = QLabel("Volume 85")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setSingleStep(1)
        self.volume_slider.setPageStep(5)
        self.volume_slider.setFixedWidth(180)
        self.volume_slider.setToolTip("Controls the volume of the active tile only")
        self.volume_slider.valueChanged.connect(self.on_volume_changed)

        tb.addWidget(self.volume_label)
        tb.addWidget(self.volume_slider)

        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer2)

        self.remove_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon), " Remove All"
        )
        self.remove_all_button.clicked.connect(self.remove_all_videos)
        tb.addWidget(self.remove_all_button)

        self.shortcut_add_all_feeds = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
        self.shortcut_add_all_feeds.activated.connect(self.add_all_feeds)

    # ---------------- State ----------------
    def save_state(self):
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "geometry": self.saveGeometry().toBase64().data().decode(),
            "videos": [(w.url, w.title) for w in self.video_widgets],
            "active_volume": int(self.active_volume),
        }
        with APP_STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)

    def load_state(self):
        if not APP_STATE_FILE.exists():
            return
        try:
            with APP_STATE_FILE.open("r", encoding="utf-8") as f:
                state = json.load(f)
            geom = state.get("geometry")
            if geom:
                self.restoreGeometry(QByteArray.fromBase64(geom.encode()))
            self.active_volume = int(state.get("active_volume", 85))
            for url, title in state.get("videos", []):
                self._enqueue(url, title)
        except Exception:
            pass

    def closeEvent(self, event):
        self.save_state()
        super().closeEvent(event)

    # ---------------- Feeds ----------------
    def load_news_feeds(self):
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with NEWS_FEEDS_FILE.open("r", encoding="utf-8") as f:
                self.news_feeds = json.load(f)
        except Exception:
            default_feeds = {
                "Associated Press": "https://www.youtube.com/channel/UC52X5wxOL_s5ywGvmcU7v8g",
                "Reuters": "https://www.youtube.com/channel/UChqUTb7kYRx8-EiaN3XFrSQ",
                "CNN": "https://www.youtube.com/@CNN",
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

    # ---------------- Queue and grid ----------------
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
        is_first_video = not bool(self.video_widgets)
        vw = QtTile(url, title, parent=self.central_widget, muted=not is_first_video)

        # connect tile signals
        vw.requestToggle.connect(self.toggle_mute_single)
        vw.started.connect(self.on_tile_playing)

        vw.remove_button.clicked.connect(lambda: self.remove_video_widget(vw))
        self.video_widgets.append(vw)

        if is_first_video:
            self.currently_unmuted = vw
            self.allow_auto_select = True

        self.update_grid()
        self._enforce_audio_policy_with_retries()

    def remove_video_widget(self, widget):
        if widget in self.video_widgets:
            was_unmuted = widget == self.currently_unmuted
            if was_unmuted:
                self.currently_unmuted = None

            self.video_widgets.remove(widget)
            widget.stop()
            widget.deleteLater()

            if was_unmuted and self.video_widgets:
                self.currently_unmuted = self.video_widgets[0]
                self.allow_auto_select = True

            self.update_grid()
            self._enforce_audio_policy_with_retries()

    def remove_all_videos(self):
        self.currently_unmuted = None
        for w in list(self.video_widgets):
            self.remove_video_widget(w)

    def update_grid(self):
        widgets_in_layout = set()
        for i in range(self.grid_layout.count()):
            item = self.grid_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), QtTile):
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

    # ---------------- Single speaker policy ----------------
    def toggle_mute_single(self, video_widget: QtTile):
        if self.currently_unmuted == video_widget:
            video_widget.set_mute_state(True)
            self.currently_unmuted = None
            self.allow_auto_select = False
        else:
            self.currently_unmuted = video_widget
            self.allow_auto_select = True
        self._enforce_audio_policy_with_retries()

    def on_tile_playing(self, tile: QtTile):
        if self.currently_unmuted is None and self.allow_auto_select:
            self.currently_unmuted = tile
        self._enforce_audio_policy_with_retries()

    def _enforce_audio_policy(self, generation: int):
        if generation != self._audio_enforce_generation:
            return
        active = self.currently_unmuted
        for tile in self.video_widgets:
            if tile is not active:
                tile.set_mute_state(True)
        if active:
            active.set_audio_for_active(self.active_volume)

    def _enforce_audio_policy_with_retries(self):
        self._audio_enforce_generation += 1
        gen = self._audio_enforce_generation
        self._enforce_audio_policy(gen)
        for i in range(1, self.AUDIO_RETRY_COUNT + 1):
            QTimer.singleShot(self.AUDIO_RETRY_DELAY_MS * i, lambda g=gen: self._enforce_audio_policy(g))

    # ---------------- Volume UI ----------------
    def on_volume_changed(self, value: int):
        self.active_volume = int(value)
        self.volume_label.setText(f"Volume {self.active_volume}")
        self._enforce_audio_policy_with_retries()

# ---------------- Entry point ----------------

def main():
    app = QApplication(sys.argv)
    win = NewsBoardVLC()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
