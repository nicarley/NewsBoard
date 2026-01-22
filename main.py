from __future__ import annotations

import json
import logging
import math
import re
import shutil
import sys
import threading
from collections import deque
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlparse

import requests
from PyQt6.QtCore import (
    QByteArray,
    QCoreApplication,
    QEvent,
    QTimer,
    QUrl,
    Qt,
    QStandardPaths,
    QSize,
    QObject,
    pyqtSignal,
    qInstallMessageHandler,
)
from PyQt6.QtGui import (
    QAction,
    QIcon,
    QKeySequence,
    QPalette,
    QColor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QWidget,
    QWidgetAction,
    QVBoxLayout,
    QInputDialog,
)

from PyQt6.QtMultimedia import (
    QAudioOutput,
    QMediaPlayer,
)
from PyQt6.QtMultimediaWidgets import QVideoWidget

try:
    from yt_dlp import YoutubeDL

    YT_AVAILABLE = True
except Exception:
    YT_AVAILABLE = False


APP_NAME = "NewsBoard"
APP_ORG = "Farleyman.com"
APP_DOMAIN = "Farleyman.com"
APP_VERSION = "26.01.21"


def tr(ctx: str, text: str) -> str:
    return QCoreApplication.translate(ctx, text)


def user_app_dir() -> Path:
    loc = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    base = Path(loc) if loc else Path.home() / f".{APP_NAME.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_files() -> Tuple[Path, Path, Path, Path]:
    base = user_app_dir()
    feeds = base / "news_feeds.json"
    playlists = base / "playlists.json"
    state = base / "app_state.json"
    log = base / "newsboard.log"
    return feeds, playlists, state, log


def migrate_legacy_settings() -> None:
    legacy = Path("resources") / "settings"
    feeds, playlists, state, _log = default_files()
    try:
        if legacy.exists() and legacy.is_dir():
            src_feeds = legacy / "news_feeds.json"
            src_state = legacy / "app_state.json"
            if src_feeds.exists() and not feeds.exists():
                feeds.write_bytes(src_feeds.read_bytes())
            if src_state.exists() and not state.exists():
                state.write_bytes(src_state.read_bytes())
    except Exception:
        pass


def setup_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def safe_disconnect(signal, slot=None) -> None:
    try:
        if slot is None:
            signal.disconnect()
        else:
            signal.disconnect(slot)
    except Exception:
        pass


YOUTUBE_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube.com",
    "www.youtube-nocookie.com",
)


class SilentLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def is_youtube_url(u: str) -> bool:
    try:
        netloc = urlparse(u).netloc.lower()
    except ValueError:
        return False
    return any(netloc == h or netloc.endswith("." + h) for h in YOUTUBE_HOSTS)


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
        return f"https://www.youtube.com/embed/{vid}"

    if "youtube.com" in netloc and path == "/watch":
        qs = dict(parse_qsl(p.query))
        v = qs.get("v", "")
        if len(v) == 11:
            return f"https://www.youtube.com/embed/{v}"

    return url


def resolve_youtube_to_direct(url: str) -> Optional[str]:
    if not YT_AVAILABLE:
        return None
    ydl_opts = {
        "quiet": True,
        "noprogress": True,
        "format": "best[acodec!=none][protocol*=http]/best[acodec!=none]/best",
        "nocookie": True,
        "cachedir": False,
        "logger": SilentLogger(),
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


def parse_m3u(content: str) -> list[Tuple[str, str]]:
    channels: list[Tuple[str, str]] = []
    lines = [line.strip() for line in content.splitlines()]

    if not lines or not lines[0].startswith("#EXTM3U"):
        return [("Pasted Link", line) for line in lines if line and not line.startswith("#")]

    i = 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF:"):
            info_line = lines[i]
            url_line = ""
            for j in range(i + 1, len(lines)):
                if lines[j] and not lines[j].startswith("#"):
                    url_line = lines[j]
                    i = j
                    break

            if url_line:
                name_match = re.search(r",(.+)$", info_line)
                name = name_match.group(1) if name_match else "Unnamed Channel"
                tvg_name_match = re.search(r'tvg-name="([^"]+)"', info_line)
                if tvg_name_match:
                    name = tvg_name_match.group(1)
                channels.append((name.strip(), url_line.strip()))
        i += 1

    return channels


@dataclass
class AppSettings:
    audio_policy: str = "single"
    volume_default: int = 85
    yt_mode: str = "direct_when_possible"
    per_host_preferred_backend: Dict[str, str] = field(default_factory=dict)
    privacy_embed_only_youtube: bool = False
    pause_others_in_fullscreen: bool = True
    theme: str = "system"
    layout_mode: str = "auto"
    first_run_done: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=4)

    @staticmethod
    def from_dict(d: Dict) -> "AppSettings":
        s = AppSettings()
        s.audio_policy = d.get("audio_policy", s.audio_policy)
        s.volume_default = int(d.get("volume_default", s.volume_default))
        s.yt_mode = d.get("yt_mode", s.yt_mode)
        s.per_host_preferred_backend = d.get("per_host_preferred_backend", {}) or {}
        s.privacy_embed_only_youtube = bool(d.get("privacy_embed_only_youtube", s.privacy_embed_only_youtube))
        s.pause_others_in_fullscreen = bool(d.get("pause_others_in_fullscreen", s.pause_others_in_fullscreen))
        s.theme = d.get("theme", s.theme)
        s.layout_mode = d.get("layout_mode", s.layout_mode)
        s.first_run_done = bool(d.get("first_run_done", s.first_run_done))
        return s


class SettingsManager:
    def __init__(self, logger: logging.Logger):
        feeds, playlists, state, log = default_files()
        self.feeds_file = feeds
        self.playlists_file = playlists
        self.state_file = state
        self.log_file = log
        self.settings_file = user_app_dir() / "settings.json"
        self.settings = AppSettings()
        self.logger = logger

    def load(self):
        try:
            if self.settings_file.exists():
                d = json.loads(self.settings_file.read_text(encoding="utf-8"))
                self.settings = AppSettings.from_dict(d)
        except Exception:
            self.logger.exception("Failed to load settings")

    def save(self):
        try:
            self.settings_file.write_text(self.settings.to_json(), encoding="utf-8")
        except Exception:
            self.logger.exception("Failed to save settings")

    def export_profile(self, parent: QWidget):
        path, _ = QFileDialog.getSaveFileName(parent, tr("Settings", "Export profile"), "", "JSON (*.json)")
        if not path:
            return
        bundle = {
            "settings": asdict(self.settings),
            "feeds": self._read_json(self.feeds_file, {}),
            "playlists": self._read_json(self.playlists_file, {}),
            "state": self._read_json(self.state_file, {}),
            "version": APP_VERSION,
        }
        try:
            Path(path).write_text(json.dumps(bundle, indent=4), encoding="utf-8")
        except Exception:
            self.logger.exception("Failed to export profile")

    def import_profile(self, parent: QWidget) -> bool:
        dialog = QFileDialog(parent, tr("Settings", "Import profile"), "", "JSON (*.json)")
        if platform_name() == "macOS":
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        if dialog.exec():
            path = dialog.selectedFiles()[0]
            try:
                bundle = json.loads(Path(path).read_text(encoding="utf-8"))
                settings = bundle.get("settings")
                if settings is not None:
                    self.settings = AppSettings.from_dict(settings)
                    self.save()
                feeds = bundle.get("feeds")
                if feeds is not None:
                    self._write_json(self.feeds_file, feeds)
                playlists = bundle.get("playlists")
                if playlists is not None:
                    self._write_json(self.playlists_file, playlists)
                state = bundle.get("state")
                if state is not None:
                    self._write_json(self.state_file, state)
                return True
            except Exception:
                self.logger.exception("Failed to import profile")
                QMessageBox.warning(parent, APP_NAME, tr("Settings", "Could not import profile"))
                return False
        return False

    @staticmethod
    def _read_json(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _write_json(path: Path, data):
        try:
            path.write_text(json.dumps(data, indent=4), encoding="utf-8")
        except Exception:
            pass


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    return "Linux"


def have_gstreamer() -> bool:
    if platform_name() != "Linux":
        return True
    return shutil.which("gst-inspect-1.0") is not None


def qt_can_play_hls() -> bool:
    try:
        mimes = QMediaPlayer.supportedMimeTypes()
        return any("application/vnd.apple.mpegurl" in m for m in mimes) or any("vnd.apple.mpegurl" in m for m in mimes)
    except Exception:
        return False


def choose_backend(url: str, settings: AppSettings) -> str:
    host = urlparse(url).netloc.lower()
    if host in settings.per_host_preferred_backend:
        return settings.per_host_preferred_backend[host]
    if is_youtube_url(url):
        return "embed" if settings.privacy_embed_only_youtube else "qt"
    return "qt"


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
                self.failed.emit("Could not resolve YouTube")

        t = threading.Thread(target=run, daemon=True)
        t.start()


class QtTile(QFrame):
    requestToggle = pyqtSignal(object)
    started = pyqtSignal(object)
    requestFullscreen = pyqtSignal(object)
    requestRemove = pyqtSignal(object)
    requestPip = pyqtSignal(object)
    requestActive = pyqtSignal(object)
    titleChanged = pyqtSignal(str)

    def __init__(self, url: str, title: str, settings: AppSettings, parent=None, muted: bool = True):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("tile")
        self.url = url
        self.title = title or tr("Tile", "Video")
        self.is_muted = muted
        self.is_fullscreen_tile = False
        self.is_pip_tile = False
        self._settings = settings
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.is_playing = False
        self._stall_retries = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.video_widget = QVideoWidget(self)
        self.video_widget.setObjectName("video")
        self.video_widget.setMinimumSize(200, 112)
        self.video_widget.setAccessibleName(tr("Tile", "Video surface"))
        self.video_widget.installEventFilter(self)
        outer.addWidget(self.video_widget, 1)

        self.status_overlay = QLabel(self.video_widget)
        self.status_overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 160); color: white; padding: 4px; font-weight: bold;"
        )
        self.status_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_overlay.hide()

        self.overlay_mute_button = QPushButton(self.video_widget)
        self.overlay_mute_button.setFixedSize(28, 28)
        self.overlay_mute_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overlay_mute_button.setStyleSheet(
            "background-color: rgba(0, 0, 0, 120); border-radius: 4px; border: 1px solid rgba(255, 255, 255, 50);"
        )
        self.overlay_mute_button.clicked.connect(lambda: self.requestToggle.emit(self))
        self.overlay_mute_button.show()

        controls = QWidget(self)
        controls.setObjectName("controls")
        controls.setAccessibleName(tr("Tile", "Controls"))
        row = QHBoxLayout(controls)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(6)

        self.label = QLabel(self.title, controls)
        self.label.setAccessibleName(tr("Tile", "Title"))
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.play_button = QPushButton("", controls)
        self.play_button.setAccessibleName(tr("Tile", "Play or pause"))
        self.play_button.setFixedSize(24, 24)
        self.play_button.setIconSize(QSize(16, 16))

        self.mute_button = QPushButton("", controls)
        self.mute_button.setAccessibleName(tr("Tile", "Mute"))
        self.mute_button.setFixedSize(24, 24)
        self.mute_button.setIconSize(QSize(16, 16))

        self.reload_button = QPushButton("", controls)
        self.reload_button.setAccessibleName(tr("Tile", "Reload"))
        self.reload_button.setFixedSize(24, 24)
        self.reload_button.setIconSize(QSize(16, 16))

        self.pip_button = QPushButton("", controls)
        self.pip_button.setAccessibleName(tr("Tile", "Picture-in-Picture"))
        self.pip_button.setFixedSize(24, 24)
        self.pip_button.setIconSize(QSize(16, 16))

        self.fullscreen_button = QPushButton("", controls)
        self.fullscreen_button.setAccessibleName(tr("Tile", "Fullscreen"))
        self.fullscreen_button.setFixedSize(24, 24)
        self.fullscreen_button.setIconSize(QSize(16, 16))

        self.remove_button = QPushButton("", controls)
        self.remove_button.setAccessibleName(tr("Tile", "Remove"))
        self.remove_button.setFixedSize(24, 24)
        self.remove_button.setIconSize(QSize(16, 16))

        row.addWidget(self.label)
        row.addStretch()
        row.addWidget(self.play_button)
        row.addWidget(self.mute_button)
        row.addWidget(self.reload_button)
        row.addWidget(self.pip_button)
        row.addWidget(self.fullscreen_button)
        row.addWidget(self.remove_button)
        outer.addWidget(controls, 0)

        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget)

        self.audio.setMuted(self.is_muted)
        self.audio.setVolume(0.0 if self.is_muted else float(self._settings.volume_default) / 100.0)

        self.player.playbackStateChanged.connect(self.on_playback_state_changed)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)

        self._apply_tile_styles()
        self._refresh_icons()

        self.play_button.clicked.connect(self.toggle_play)
        self.mute_button.clicked.connect(lambda: self.requestToggle.emit(self))
        self.reload_button.clicked.connect(self.safe_reload)
        self.pip_button.clicked.connect(lambda: self.requestPip.emit(self))
        self.fullscreen_button.clicked.connect(lambda: self.requestFullscreen.emit(self))
        self.remove_button.clicked.connect(lambda: self.requestRemove.emit(self))

        self.play_url(self.url)

    def _apply_tile_styles(self):
        self.setStyleSheet(
            """
            QFrame#tile { border-radius: 10px; background: rgba(20, 20, 20, 255); }
            QWidget#controls { background: rgba(0, 0, 0, 140); }
            QLabel { padding-left: 6px; color: white; }
            QWidget#controls QPushButton { border: none; padding: 0px; margin: 0px; background: transparent; }
            """
        )

    def eventFilter(self, obj, event):
        if obj == self.video_widget:
            if event.type() == QEvent.Type.Resize:
                self._reposition_overlays()
            elif event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    self.requestActive.emit(self)
        return super().eventFilter(obj, event)

    def _reposition_overlays(self):
        w = self.video_widget.width()
        h = self.video_widget.height()
        if self.status_overlay:
            self.status_overlay.setGeometry(0, 0, w, h)
        if hasattr(self, "overlay_mute_button"):
            self.overlay_mute_button.move(w - self.overlay_mute_button.width() - 8, 8)

    def contextMenuEvent(self, event: QEvent):
        menu = QMenu(self)
        style = self.style()

        mute_text = tr("Tile", "Unmute") if self.is_muted else tr("Tile", "Mute")
        mute_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_MediaVolume if self.is_muted else QStyle.StandardPixmap.SP_MediaVolumeMuted
        )
        mute_action = QAction(mute_icon, mute_text, self)
        mute_action.triggered.connect(lambda: self.requestToggle.emit(self))
        menu.addAction(mute_action)

        pip_text = tr("Tile", "Exit Picture-in-Picture") if self.is_pip_tile else tr("Tile", "Enter Picture-in-Picture")
        pip_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_TitleBarNormalButton
            if self.is_pip_tile
            else QStyle.StandardPixmap.SP_TitleBarMinButton
        )
        pip_action = QAction(pip_icon, pip_text, self)
        pip_action.triggered.connect(lambda: self.requestPip.emit(self))
        menu.addAction(pip_action)

        fullscreen_text = tr("Tile", "Exit Fullscreen") if self.is_fullscreen_tile else tr("Tile", "Enter Fullscreen")
        fullscreen_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_TitleBarNormalButton
            if self.is_fullscreen_tile
            else QStyle.StandardPixmap.SP_TitleBarMaxButton
        )
        fullscreen_action = QAction(fullscreen_icon, fullscreen_text, self)
        fullscreen_action.triggered.connect(lambda: self.requestFullscreen.emit(self))
        menu.addAction(fullscreen_action)

        reload_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload), tr("Tile", "Reload"), self)
        reload_action.triggered.connect(self.safe_reload)
        menu.addAction(reload_action)

        copy_url_action = QAction(tr("Tile", "Copy URL"), self)
        copy_url_action.triggered.connect(self.copy_url)
        menu.addAction(copy_url_action)

        rename_action = QAction(tr("Tile", "Rename"), self)
        rename_action.triggered.connect(self.rename_tile)
        menu.addAction(rename_action)

        menu.addSeparator()

        remove_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), tr("Tile", "Remove"), self)
        remove_action.triggered.connect(lambda: self.requestRemove.emit(self))
        menu.addAction(remove_action)

        menu.exec(event.globalPos())

    def copy_url(self):
        QApplication.clipboard().setText(self.url)

    def _set_status_overlay(self, text: str):
        if text:
            self.status_overlay.setText(text)
            self.status_overlay.show()
        else:
            self.status_overlay.hide()

    def on_media_status_changed(self, status: QMediaPlayer.MediaStatus):
        if status == QMediaPlayer.MediaStatus.LoadingMedia:
            self.label.setText(f"{self.title}  {tr('Tile', 'Loading')}")
            self._set_status_overlay(tr("Tile", "Loading"))
        elif status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.label.setText(self.title)
            self._stall_retries = 0
            self._set_status_overlay("")
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.label.setText(f"{self.title}  {tr('Tile', 'Invalid media')}")
            self._stall_retries += 1
            self._set_status_overlay(tr("Tile", "Invalid media"))
        elif status == QMediaPlayer.MediaStatus.StalledMedia:
            self.label.setText(f"{self.title}  {tr('Tile', 'Stalled')}")
            self._stall_retries += 1
            self._set_status_overlay(tr("Tile", "Stalled, retrying"))
            if self._stall_retries <= 3:
                QTimer.singleShot(2000, self.safe_reload)
        elif status == QMediaPlayer.MediaStatus.BufferingMedia:
            self.label.setText(f"{self.title}  {tr('Tile', 'Buffering')}")
            self._set_status_overlay(tr("Tile", "Buffering"))

    def on_playback_state_changed(self, state: QMediaPlayer.PlaybackState):
        self.is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._refresh_icons()

    def toggle_play(self):
        state = self.player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def play_url(self, url: str):
        chosen = choose_backend(url, self._settings)
        src = build_embed_or_watch(url)

        if chosen in ("qt", "embed") and is_youtube_url(src):
            if YT_AVAILABLE:
                self.label.setText(f"{self.title}  {tr('Tile', 'Resolving')}")
                self._set_status_overlay(tr("Tile", "Resolving"))
                self._yt_worker = YtResolveWorker(src, self)
                self._yt_worker.resolved.connect(lambda direct: self._apply_source(QUrl(direct)))
                self._yt_worker.failed.connect(lambda _msg: self.label.setText(f"{self.title}  {tr('Tile', 'Resolve failed')}"))
                self._yt_worker.failed.connect(lambda _msg: self._set_status_overlay(tr("Tile", "Resolve failed")))
                self._yt_worker.start()
                return
            else:
                self.label.setText(f"{self.title}  {tr('Tile', 'yt dlp not installed')}")
                self._set_status_overlay(tr("Tile", "yt dlp not installed"))
                return

        self._apply_source(QUrl(src))

    def _apply_source(self, qurl: QUrl):
        self._stall_retries = 0
        self.player.setSource(qurl)
        self.player.play()
        QTimer.singleShot(0, lambda: self.started.emit(self))

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

    def set_mute_state(self, muted: bool):
        if muted:
            self.hard_mute()
        else:
            self.audio.setMuted(False)
            self.is_muted = False
            self._refresh_icons()

    def set_is_fullscreen(self, is_fullscreen: bool):
        self.is_fullscreen_tile = is_fullscreen
        self._refresh_icons()

    def set_is_pip(self, is_pip: bool):
        self.is_pip_tile = is_pip
        self._refresh_icons()

    def _refresh_icons(self):
        style = self.style()
        icon = style.standardIcon(
            QStyle.StandardPixmap.SP_MediaVolumeMuted if self.is_muted else QStyle.StandardPixmap.SP_MediaVolume
        )
        self.mute_button.setIcon(icon)
        if hasattr(self, "overlay_mute_button"):
            self.overlay_mute_button.setIcon(icon)

        self.play_button.setIcon(
            style.standardIcon(
                QStyle.StandardPixmap.SP_MediaPlay if not self.is_playing else QStyle.StandardPixmap.SP_MediaPause
            )
        )
        self.reload_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload))

        if self.is_pip_tile:
            self.pip_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton))
            self.pip_button.setToolTip(tr("Tile", "Exit Picture-in-Picture"))
        else:
            self.pip_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_TitleBarMinButton))
            self.pip_button.setToolTip(tr("Tile", "Enter Picture-in-Picture"))

        if self.is_fullscreen_tile:
            self.fullscreen_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton))
            self.fullscreen_button.setToolTip(tr("Tile", "Exit Fullscreen"))
        else:
            self.fullscreen_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton))
            self.fullscreen_button.setToolTip(tr("Tile", "Enter Fullscreen"))

        self.remove_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon))

    def safe_reload(self):
        try:
            self.player.stop()
        except Exception:
            pass

        safe_disconnect(self.player.mediaStatusChanged, self.on_media_status_changed)
        safe_disconnect(self.player.playbackStateChanged, self.on_playback_state_changed)

        try:
            self.player.deleteLater()
        except Exception:
            pass

        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.player.playbackStateChanged.connect(self.on_playback_state_changed)

        if self.is_muted:
            self.audio.setMuted(True)
            self.audio.setVolume(0.0)

        self.play_url(self.url)

    def stop(self):
        safe_disconnect(self.player.playbackStateChanged)
        safe_disconnect(self.player.mediaStatusChanged)
        safe_disconnect(self.mute_button.clicked)
        safe_disconnect(self.reload_button.clicked)
        safe_disconnect(self.pip_button.clicked)
        safe_disconnect(self.fullscreen_button.clicked)
        safe_disconnect(self.remove_button.clicked)

        try:
            self.player.stop()
        except Exception:
            pass
        try:
            self.player.setVideoOutput(None)
        except Exception:
            pass
        try:
            self.player.setAudioOutput(None)
        except Exception:
            pass
        try:
            self.player.setSource(QUrl())
        except Exception:
            pass
        try:
            self.audio.deleteLater()
        except Exception:
            pass
        try:
            self.player.deleteLater()
        except Exception:
            pass

    def rename_tile(self):
        new_title, ok = QInputDialog.getText(self, tr("Tile", "Rename"), tr("Tile", "New title"), text=self.title)
        if ok:
            new_title = new_title.strip()
            if new_title:
                self.title = new_title
                self.label.setText(self.title)
                self.titleChanged.emit(self.title)

    def closeEvent(self, e):
        self.stop()
        super().closeEvent(e)


class FeedDialog(QDialog):
    def __init__(self, parent=None, name="", url=""):
        super().__init__(parent)
        self.setWindowTitle(tr("Feed", "Add or Edit Feed"))
        form = QFormLayout(self)
        self.name_input = QLineEdit(name)
        self.url_input = QLineEdit(url)
        self.name_label = QLabel(tr("Feed", "Name"))
        form.addRow(self.name_label, self.name_input)
        form.addRow(tr("Feed", "URL"), self.url_input)
        box = QDialogButtonBox()
        box.setStandardButtons(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addWidget(box)

    def set_name_label(self, text: str):
        self.name_label.setText(text)

    def get_feed_data(self):
        return self.name_input.text(), self.url_input.text()


class PlaylistViewerDialog(QDialog):
    def __init__(self, parent, channels):
        super().__init__(parent)
        self.setWindowTitle(tr("Playlist", "Select Channels"))
        self.setMinimumSize(400, 500)
        layout = QVBoxLayout(self)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for name, url in channels:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, url)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def get_selected_channels(self):
        selected_channels = []
        for item in self.list_widget.selectedItems():
            name = item.text()
            url = item.data(Qt.ItemDataRole.UserRole)
            selected_channels.append((name, url))
        return selected_channels


class ListManager(QDockWidget):
    def __init__(self, parent=None):
        super().__init__(tr("List", "Manage Lists"), parent)
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.setObjectName("listManager")

        self.tabs = QTabWidget()
        self.setWidget(self.tabs)

        style = self.style()

        self.news_feed_tab = QWidget()
        self.news_feed_layout = QVBoxLayout(self.news_feed_tab)
        self.news_feed_layout.setContentsMargins(6, 6, 6, 6)
        self.news_feed_layout.setSpacing(6)

        self.feed_search = QLineEdit()
        self.feed_search.setPlaceholderText(tr("List", "Filter feeds"))
        self.feed_search.textChanged.connect(self.filter_feeds)
        self.news_feed_layout.addWidget(self.feed_search)

        self.toolbar = QWidget()
        self.toolbar_layout = QHBoxLayout(self.toolbar)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout.setSpacing(6)

        self.add_feed_to_grid_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton), tr("List", "Add Selected")
        )
        self.add_all_feeds_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward), tr("List", "Add All"))
        self.add_new_feed_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_FileIcon), tr("List", "New"))
        self.edit_feed_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_DesktopIcon), tr("List", "Edit"))
        self.remove_feed_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), tr("List", "Remove"))
        self.remove_all_feeds_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_BrowserStop), tr("List", "Clear All"))

        for b in (
            self.add_feed_to_grid_button,
            self.add_all_feeds_button,
            self.add_new_feed_button,
            self.edit_feed_button,
            self.remove_feed_button,
            self.remove_all_feeds_button,
        ):
            b.setAccessibleName(b.text())

        self.toolbar_layout.addWidget(self.add_feed_to_grid_button)
        self.toolbar_layout.addWidget(self.add_all_feeds_button)
        self.toolbar_layout.addSpacing(8)
        self.toolbar_layout.addWidget(self.add_new_feed_button)
        self.toolbar_layout.addWidget(self.edit_feed_button)
        self.toolbar_layout.addWidget(self.remove_feed_button)
        self.toolbar_layout.addStretch()
        self.toolbar_layout.addWidget(self.remove_all_feeds_button)

        self.news_feed_list_widget = QListWidget()
        self.news_feed_list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.news_feed_list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.news_feed_list_widget.setDragEnabled(True)
        self.news_feed_list_widget.setAcceptDrops(True)
        self.news_feed_list_widget.setDropIndicatorShown(True)
        self.news_feed_list_widget.setSizePolicy(
            self.news_feed_list_widget.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Expanding,
        )
        self.news_feed_list_widget.setAccessibleName(tr("List", "Feeds"))

        self.news_feed_layout.addWidget(self.toolbar, 0)
        self.news_feed_layout.addWidget(self.news_feed_list_widget, 1)
        self.tabs.addTab(self.news_feed_tab, tr("List", "News Feeds"))

        self.playlists_tab = QWidget()
        self.playlists_layout = QVBoxLayout(self.playlists_tab)
        self.playlists_layout.setContentsMargins(6, 6, 6, 6)
        self.playlists_layout.setSpacing(6)

        self.playlist_search = QLineEdit()
        self.playlist_search.setPlaceholderText(tr("List", "Filter playlists"))
        self.playlist_search.textChanged.connect(self.filter_playlists)
        self.playlists_layout.addWidget(self.playlist_search)

        self.playlists_toolbar = QWidget()
        self.playlists_toolbar_layout = QHBoxLayout(self.playlists_toolbar)
        self.playlists_toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.playlists_toolbar_layout.setSpacing(6)

        self.add_playlist_to_grid_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton), tr("List", "Add Selected")
        )
        self.add_all_playlist_channels_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward), tr("List", "Add All")
        )
        self.view_playlist_channels_button = QPushButton(
            style.standardIcon(QStyle.StandardPixmap.SP_DesktopIcon), tr("List", "View Channels")
        )
        self.add_playlist_url_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_FileIcon), tr("List", "Add URL"))
        self.remove_playlist_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), tr("List", "Remove"))
        self.remove_all_playlists_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_BrowserStop), tr("List", "Clear All"))

        self.playlists_toolbar_layout.addWidget(self.add_playlist_to_grid_button)
        self.playlists_toolbar_layout.addWidget(self.add_all_playlist_channels_button)
        self.playlists_toolbar_layout.addWidget(self.view_playlist_channels_button)
        self.playlists_toolbar_layout.addSpacing(8)
        self.playlists_toolbar_layout.addWidget(self.add_playlist_url_button)
        self.playlists_toolbar_layout.addWidget(self.remove_playlist_button)
        self.playlists_toolbar_layout.addStretch()
        self.playlists_toolbar_layout.addWidget(self.remove_all_playlists_button)

        self.playlists_list_widget = QListWidget()
        self.playlists_list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.playlists_list_widget.setAccessibleName(tr("List", "Playlists"))

        self.playlists_layout.addWidget(self.playlists_toolbar, 0)
        self.playlists_layout.addWidget(self.playlists_list_widget, 1)
        self.tabs.addTab(self.playlists_tab, tr("List", "Playlists"))

        self.grid_tab = QWidget()
        self.grid_layout = QVBoxLayout(self.grid_tab)
        self.grid_layout.setContentsMargins(6, 6, 6, 6)
        self.grid_layout.setSpacing(6)

        self.grid_toolbar = QWidget()
        self.grid_toolbar_layout = QHBoxLayout(self.grid_toolbar)
        self.grid_toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_toolbar_layout.setSpacing(6)

        self.remove_from_grid_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), tr("List", "Remove"))
        self.remove_from_grid_button.setAccessibleName(tr("List", "Remove from grid"))
        self.grid_toolbar_layout.addWidget(self.remove_from_grid_button)
        self.grid_toolbar_layout.addStretch()

        self.grid_list_widget = QListWidget()
        self.grid_list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid_list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.grid_list_widget.setDragEnabled(True)
        self.grid_list_widget.setAcceptDrops(True)
        self.grid_list_widget.setDropIndicatorShown(True)
        self.grid_list_widget.setAccessibleName(tr("List", "Grid Items"))

        self.grid_layout.addWidget(self.grid_toolbar, 0)
        self.grid_layout.addWidget(self.grid_list_widget, 1)
        self.tabs.addTab(self.grid_tab, tr("List", "Grid"))

    def filter_feeds(self, text: str):
        text = text.lower().strip()
        for i in range(self.news_feed_list_widget.count()):
            item = self.news_feed_list_widget.item(i)
            name = item.text().lower()
            item.setHidden(bool(text and text not in name))

    def filter_playlists(self, text: str):
        text = text.lower().strip()
        for i in range(self.playlists_list_widget.count()):
            item = self.playlists_list_widget.item(i)
            name = item.text().lower()
            item.setHidden(bool(text and text not in name))


class DiagnosticsDialog(QDialog):
    def __init__(self, parent, settings: SettingsManager):
        super().__init__(parent)
        self.setWindowTitle(tr("Diag", "Diagnostics"))
        lay = QVBoxLayout(self)

        feeds, playlists, state, log = (
            settings.feeds_file,
            settings.playlists_file,
            settings.state_file,
            settings.log_file,
        )

        lines = []
        lines.append(f"{APP_NAME} {APP_VERSION}")
        lines.append(f"Platform: {platform_name()}")
        lines.append(f"Qt multimedia HLS probe: {'yes' if qt_can_play_hls() else 'no'}")
        if platform_name() == "Linux":
            lines.append(f"GStreamer present: {'yes' if have_gstreamer() else 'no'}")
        lines.append(f"App data folder: {user_app_dir()}")
        lines.append(f"Feeds file: {feeds}  exists={feeds.exists()}")
        lines.append(f"Playlists file: {playlists}  exists={playlists.exists()}")
        lines.append(f"State file: {state}  exists={state.exists()}")
        lines.append(f"Log file: {log}  exists={log.exists()}")
        lines.append(f"yt dlp available: {'yes' if YT_AVAILABLE else 'no'}")

        tabs = QTabWidget(self)

        info_text = QTextEdit()
        info_text.setReadOnly(True)
        info_text.setPlainText("\n".join(lines))
        tabs.addTab(info_text, tr("Diag", "Info"))

        log_text = QTextEdit()
        log_text.setReadOnly(True)
        try:
            if log.exists():
                content = log.read_text(encoding="utf-8", errors="ignore")
                if len(content) > 20000:
                    content = content[-20000:]
                log_text.setPlainText(content)
            else:
                log_text.setPlainText(tr("Diag", "Log file does not yet exist."))
        except Exception:
            log_text.setPlainText(tr("Diag", "Could not read log file."))
        tabs.addTab(log_text, tr("Diag", "Log"))

        lay.addWidget(tabs)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_btn = QPushButton(tr("Diag", "Copy report"))
        btns.addButton(copy_btn, QDialogButtonBox.ButtonRole.ActionRole)

        def copy_report():
            combined = info_text.toPlainText() + "\n\n" + log_text.toPlainText()
            QApplication.clipboard().setText(combined)

        copy_btn.clicked.connect(copy_report)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        lay.addWidget(btns)


class SettingsDialog(QDialog):
    def __init__(self, parent, sm: SettingsManager):
        super().__init__(parent)
        self.setWindowTitle(tr("Settings", "Settings"))
        self._sm = sm
        s = sm.settings
        self.profile_imported = False

        form = QFormLayout(self)

        self.audio_policy = QComboBox()
        self.audio_policy.addItems(["single", "mixed"])

        self.volume_default = QSlider(Qt.Orientation.Horizontal)
        self.volume_default.setRange(0, 100)

        self.yt_mode = QComboBox()
        self.yt_mode.addItems(["direct_when_possible", "embed_only"])

        self.privacy_embed_only_youtube = QCheckBox(tr("Settings", "Use embeds for YouTube"))
        self.pause_others_in_fullscreen = QCheckBox(tr("Settings", "Pause non fullscreen tiles"))

        self.theme = QComboBox()
        self.theme.addItems(["system", "light", "dark"])

        self.layout_mode = QComboBox()
        self.layout_mode.addItems(["auto", "2x2", "3x3", "1xN", "Nx1"])

        self.populate_fields()

        form.addRow(tr("Settings", "Audio policy"), self.audio_policy)
        form.addRow(tr("Settings", "Default volume"), self.volume_default)
        form.addRow(tr("Settings", "YouTube mode"), self.yt_mode)
        form.addRow(tr("Settings", "Theme"), self.theme)
        form.addRow(tr("Settings", "Layout"), self.layout_mode)
        form.addRow("", self.privacy_embed_only_youtube)
        form.addRow("", self.pause_others_in_fullscreen)

        line = QHBoxLayout()
        self.btn_export = QPushButton(tr("Settings", "Export profile"))
        self.btn_import = QPushButton(tr("Settings", "Import profile"))
        self.btn_export.clicked.connect(lambda: sm.export_profile(self))
        self.btn_import.clicked.connect(self.do_import)
        line.addWidget(self.btn_export)
        line.addWidget(self.btn_import)
        box = QWidget()
        box.setLayout(line)
        form.addRow("", box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

    def populate_fields(self):
        s = self._sm.settings
        self.audio_policy.setCurrentText(s.audio_policy)
        self.volume_default.setValue(s.volume_default)
        self.yt_mode.setCurrentText(s.yt_mode)
        self.privacy_embed_only_youtube.setChecked(s.privacy_embed_only_youtube)
        self.pause_others_in_fullscreen.setChecked(s.pause_others_in_fullscreen)
        self.theme.setCurrentText(s.theme)
        if s.layout_mode in ["auto", "2x2", "3x3", "1xN", "Nx1"]:
            self.layout_mode.setCurrentText(s.layout_mode)
        else:
            self.layout_mode.setCurrentText("auto")

    def do_import(self):
        if self._sm.import_profile(self):
            self.profile_imported = True
            self.accept()

    def apply(self):
        s = self._sm.settings
        s.audio_policy = self.audio_policy.currentText()
        s.volume_default = int(self.volume_default.value())
        s.yt_mode = self.yt_mode.currentText()
        s.privacy_embed_only_youtube = bool(self.privacy_embed_only_youtube.isChecked())
        s.pause_others_in_fullscreen = bool(self.pause_others_in_fullscreen.isChecked())
        s.theme = self.theme.currentText()
        s.layout_mode = self.layout_mode.currentText()
        self._sm.save()


class PipWindow(QWidget):
    def __init__(self, tile: QtTile, main_window: QMainWindow):
        super().__init__()
        self.tile = tile
        self.main_window = main_window
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setWindowTitle(f"PiP  {tile.title}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tile)

        self.tile.set_is_pip(True)
        self.show()

    def closeEvent(self, event):
        self.tile.set_is_pip(False)
        self.layout().removeWidget(self.tile)
        self.tile.setParent(None)
        self.main_window.reattach_tile(self.tile)
        super().closeEvent(event)


class NewsBoard(QMainWindow):
    AUDIO_RETRY_COUNT = 6
    AUDIO_RETRY_DELAY_MS = 150

    def __init__(self, sm: SettingsManager):
        super().__init__()
        self.sm = sm
        self.logger = sm.logger
        self.settings = sm.settings
        self.window_state_before_fullscreen = None
        self.pip_windows: Dict[QtTile, PipWindow] = {}

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(QIcon("resources/icon.ico"))
        self.setBaseSize(1280, 720)

        self.apply_theme()

        self.central_widget_container = QWidget()
        self.central_widget_container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        the_layout = QGridLayout(self.central_widget_container)
        the_layout.setContentsMargins(0, 0, 0, 0)
        the_layout.setSpacing(4)
        self.grid_layout = the_layout
        self.setCentralWidget(self.central_widget_container)

        self.status_bar = self.statusBar()
        self.status_label = QLabel()
        self.status_bar.addPermanentWidget(self.status_label)
        self.status_label.setText(tr("UI", "Ready"))

        self.video_widgets: list[QtTile] = []
        self.currently_unmuted: Optional[QtTile] = None
        self.active_volume: int = int(self.settings.volume_default)
        self.allow_auto_select: bool = True
        self._audio_enforce_generation = 0
        self.fullscreen_tile: Optional[QtTile] = None
        self.main_toolbar: Optional[QToolBar] = None
        self._is_clearing = False
        self.url_action: Optional[QWidgetAction] = None

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
        self.list_manager.grid_list_widget.model().rowsMoved.connect(self.reorder_video_widgets)
        self.list_manager.add_playlist_url_button.clicked.connect(self.add_playlist_url)
        self.list_manager.add_playlist_to_grid_button.clicked.connect(self.add_playlist_to_grid)
        self.list_manager.add_all_playlist_channels_button.clicked.connect(self.add_all_playlist_channels)
        self.list_manager.view_playlist_channels_button.clicked.connect(self.view_playlist_channels)
        self.list_manager.remove_playlist_button.clicked.connect(self.remove_playlist)
        self.list_manager.remove_all_playlists_button.clicked.connect(self.remove_all_playlists)

        self._build_top_toolbar()
        self._build_menus()

        self._queue = deque()
        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(120)
        self._queue_timer.timeout.connect(self._process_queue)

        self.news_feeds: Dict[str, str] = {}
        self.playlists: Dict[str, str] = {}
        self.load_news_feeds()
        self.load_playlists()
        self.list_manager.tabs.setCurrentIndex(0)

        self.load_state()

        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(self.active_volume)
        self.volume_label.setText(tr("UI", f"Volume {self.active_volume}"))
        self.volume_slider.blockSignals(False)

        self.first_run_checklist()

        if not self.settings.first_run_done:
            QMessageBox.information(
                self,
                APP_NAME,
                tr(
                    "UI",
                    "Welcome to NewsBoard.\nUse Manage Lists to work with feeds and playlists.\nPaste a URL in the toolbar to add a single stream."
                ),
            )
            self.settings.first_run_done = True
            self.sm.save()

        self.central_widget_container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def refresh_ui(self):
        self.sm.load()
        self.settings = self.sm.settings
        self.apply_theme()
        self.load_news_feeds()
        self.load_playlists()
        self.remove_all_videos()
        self.load_state()

    def apply_theme(self):
        app = QApplication.instance()
        if self.settings.theme == "dark":
            dark_palette = QPalette()
            dark_palette.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0))
            dark_palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
            dark_palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
            dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
            dark_palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
            dark_palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.black)
            dark_palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
            dark_palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
            dark_palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
            dark_palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
            dark_palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
            dark_palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
            dark_palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
            app.setPalette(dark_palette)
            app.setStyleSheet("""
                QToolTip { color: #ffffff; background-color: #2a82da; border: 1px solid white; }
                QMenu { background-color: #2b2b2b; color: #ffffff; border: 1px solid #3a3a3a; }
                QMenu::item { background-color: transparent; padding: 4px 20px; }
                QMenu::item:selected { background-color: #4282da; color: #ffffff; }
                QMenuBar { background-color: #1e1e1e; color: #ffffff; }
                QMenuBar::item { background-color: transparent; padding: 4px 10px; }
                QMenuBar::item:selected { background-color: #3a3a3a; }
            """)
        else:
            app.setPalette(QApplication.style().standardPalette())
            app.setStyleSheet("")

    def _build_menus(self):
        mb = self.menuBar()

        file_menu = mb.addMenu(tr("Menu", "File"))

        act_add_playlist = QAction(tr("Menu", "Add Playlist URL"), self)
        act_add_playlist.triggered.connect(self.add_playlist_url)
        file_menu.addAction(act_add_playlist)

        act_import_m3u = QAction(tr("Menu", "Import playlist from file"), self)
        act_import_m3u.triggered.connect(self.import_playlist_from_file)
        file_menu.addAction(act_import_m3u)

        act_export_grid_m3u = QAction(tr("Menu", "Export grid as M3U"), self)
        act_export_grid_m3u.triggered.connect(self.export_grid_as_m3u)
        file_menu.addAction(act_export_grid_m3u)

        file_menu.addSeparator()

        act_settings = QAction(tr("Menu", "Settings"), self)
        act_settings.triggered.connect(self.open_settings)
        file_menu.addAction(act_settings)

        act_export = QAction(tr("Menu", "Export profile"), self)
        act_export.triggered.connect(lambda: self.sm.export_profile(self))
        file_menu.addAction(act_export)

        act_import = QAction(tr("Menu", "Import profile"), self)
        act_import.triggered.connect(self.import_profile_action)
        file_menu.addAction(act_import)

        file_menu.addSeparator()
        act_quit = QAction(tr("Menu", "Quit"), self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        tools = mb.addMenu(tr("Menu", "Tools"))
        act_diag = QAction(tr("Menu", "Diagnostics"), self)
        act_diag.triggered.connect(self.open_diagnostics)
        tools.addAction(act_diag)

        helpm = mb.addMenu(tr("Menu", "Help"))
        act_help = QAction(tr("Menu", "Quick help"), self)
        act_help.setShortcut(QKeySequence.StandardKey.HelpContents)
        act_help.triggered.connect(self.open_help_overlay)
        helpm.addAction(act_help)

        act_about = QAction(tr("Menu", "About"), self)
        act_about.triggered.connect(self.open_about)
        helpm.addAction(act_about)

    def _build_top_toolbar(self):
        tb = QToolBar("Main")
        self.main_toolbar = tb
        tb.setMovable(False)
        tb.setFloatable(False)
        sz = self.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize) or 16
        tb.setIconSize(QSize(sz, sz))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)

        self.manage_lists_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ToolBarHorizontalExtensionButton),
            tr("UI", "Manage Lists"),
        )
        self.manage_lists_button.setCheckable(True)
        self.manage_lists_button.setChecked(True)
        self.manage_lists_button.setToolTip(tr("UI", "Show or hide list manager"))
        self.manage_lists_button.toggled.connect(self.list_manager.setVisible)
        self.list_manager.visibilityChanged.connect(self.manage_lists_button.setChecked)
        tb.addWidget(self.manage_lists_button)

        spacer_small = QWidget()
        spacer_small.setFixedWidth(6)
        tb.addWidget(spacer_small)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(tr("UI", "Enter URL or iframe"))
        self.url_input.setToolTip(tr("UI", "Paste a stream URL or iframe"))
        self.url_action = QWidgetAction(self)
        self.url_action.setDefaultWidget(self.url_input)
        tb.addAction(self.url_action)

        self.add_video_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton),
            tr("UI", "Add Video"),
        )
        self.add_video_button.clicked.connect(self.add_video_from_input)
        tb.addWidget(self.add_video_button)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.volume_label = QLabel(tr("UI", "Volume"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setSingleStep(1)
        self.volume_slider.setPageStep(5)
        self.volume_slider.setFixedWidth(180)
        self.volume_slider.setToolTip(tr("UI", "Controls active tile volume"))
        self.volume_slider.valueChanged.connect(self.on_volume_changed)

        tb.addWidget(self.volume_label)
        tb.addWidget(self.volume_slider)

        self.mute_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolumeMuted),
            tr("UI", "Mute All"),
        )
        self.mute_all_button.clicked.connect(self.mute_all_tiles)
        tb.addWidget(self.mute_all_button)

        self.next_audio_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward),
            tr("UI", "Next Audio"),
        )
        self.next_audio_button.clicked.connect(self.cycle_active_audio)
        tb.addWidget(self.next_audio_button)

        self.layout_combo = QComboBox()
        self.layout_combo.addItems(["auto", "2x2", "3x3", "1xN", "Nx1"])
        self.layout_combo.setToolTip(tr("UI", "Grid layout"))
        self.layout_combo.setCurrentText(self.settings.layout_mode)
        self.layout_combo.currentTextChanged.connect(self.on_layout_mode_changed)
        tb.addWidget(self.layout_combo)

        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer2)

        self.fullscreen_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton),
            tr("UI", "Fullscreen"),
        )
        self.fullscreen_button.clicked.connect(self.toggle_grid_fullscreen)
        tb.addWidget(self.fullscreen_button)

        self.reload_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
            tr("UI", "Reload All"),
        )
        self.reload_all_button.clicked.connect(self.reload_all_videos)
        tb.addWidget(self.reload_all_button)

        self.remove_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon),
            tr("UI", "Remove All"),
        )
        self.remove_all_button.clicked.connect(self.remove_all_videos)
        tb.addWidget(self.remove_all_button)

        self.shortcut_add_all_feeds = QAction(self)
        self.shortcut_add_all_feeds.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.shortcut_add_all_feeds.triggered.connect(self.add_all_feeds)
        self.addAction(self.shortcut_add_all_feeds)

        self.shortcut_delete = QAction(self)
        self.shortcut_delete.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        self.shortcut_delete.triggered.connect(self.remove_video_from_grid_list)
        self.addAction(self.shortcut_delete)

        self.shortcut_fullscreen = QAction(self)
        self.shortcut_fullscreen.setShortcut(QKeySequence("F"))
        self.shortcut_fullscreen.triggered.connect(self.toggle_first_fullscreen)
        self.addAction(self.shortcut_fullscreen)

    def on_layout_mode_changed(self, mode: str):
        self.settings.layout_mode = mode
        self.sm.save()
        self.update_grid()

    def import_profile_action(self):
        if self.sm.import_profile(self):
            self.refresh_ui()
            QMessageBox.information(self, APP_NAME, tr("Settings", "Profile imported and applied."))

    def first_run_checklist(self):
        msgs = []
        if platform_name() == "Linux" and not have_gstreamer():
            msgs.append(tr("Diag", "GStreamer is not found. Install base good bad and ugly sets."))
        if msgs:
            QMessageBox.information(self, APP_NAME, "\n".join(msgs))

    def _fetch_text(self, url: str, timeout: int = 10) -> Optional[str]:
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r.text
        except requests.exceptions.RequestException:
            return None

    def _enqueue_playlist_channels(self, m3u_text: str, choose: bool):
        channels = parse_m3u(m3u_text)
        if not channels:
            QMessageBox.information(self, "Playlist", "No channels found in this playlist.")
            return

        if choose:
            dlg = PlaylistViewerDialog(self, channels)
            if not dlg.exec():
                return
            channels = dlg.get_selected_channels()

        for name, url in channels:
            self._enqueue(url, name)

    def load_news_feeds(self):
        feeds_file, _, _, _ = default_files()
        feeds_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            feeds = json.loads(feeds_file.read_text(encoding="utf-8"))
        except Exception:
            feeds = {
                "Associated Press": "https://www.youtube.com/channel/UC52X5wxOL_s5ywGvmcU7v8g",
                "Reuters": "https://www.youtube.com/channel/UChqUTb7kYRx8-EiaN3XFrSQ",
                "CNN": "https://www.youtube.com/@CNN",
            }
            try:
                feeds_file.write_text(json.dumps(feeds, indent=4), encoding="utf-8")
            except Exception:
                pass
        self.news_feeds = feeds
        self._refresh_feeds_list()

    def _refresh_feeds_list(self):
        w = self.list_manager.news_feed_list_widget
        w.clear()
        for name, url in self.news_feeds.items():
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, url)
            w.addItem(item)

    def save_news_feeds(self):
        f, _, _, _ = default_files()
        f.parent.mkdir(parents=True, exist_ok=True)
        try:
            f.write_text(json.dumps(self.news_feeds, indent=4), encoding="utf-8")
        except Exception:
            pass

    def add_video_from_input(self):
        url_or_iframe = self.url_input.text().strip()
        if not url_or_iframe:
            return
        m = re.search(r'<iframe.*?src="([^"]*)"', url_or_iframe, re.IGNORECASE)
        url = m.group(1) if m else url_or_iframe
        self._enqueue(url, tr("UI", "Pasted Video"))
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
            name = name.strip()
            url = url.strip()
            if name and url:
                self.news_feeds[name] = url
                self.save_news_feeds()
                self._refresh_feeds_list()

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
            new_name = new_name.strip()
            new_url = new_url.strip()
            if new_name and new_url:
                self.news_feeds.pop(old_name, None)
                self.news_feeds[new_name] = new_url
                self.save_news_feeds()
                self._refresh_feeds_list()

    def remove_feed(self):
        selected = self.list_manager.news_feed_list_widget.selectedItems()
        if not selected:
            return
        for item in selected:
            name = item.text()
            self.news_feeds.pop(name, None)
        self.save_news_feeds()
        self._refresh_feeds_list()

    def remove_all_feeds(self):
        if not self.news_feeds:
            return
        reply = QMessageBox.question(
            self,
            tr("List", "Confirm Clear All"),
            tr("List", "Are you sure you want to clear all news feeds?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.No:
            return
        self.news_feeds.clear()
        self.save_news_feeds()
        self._refresh_feeds_list()

    def reorder_news_feeds(self, *_):
        new_order_names = []
        w = self.list_manager.news_feed_list_widget
        for i in range(w.count()):
            new_order_names.append(w.item(i).text())
        new_news_feeds = {}
        for name in new_order_names:
            if name in self.news_feeds:
                new_news_feeds[name] = self.news_feeds[name]
        self.news_feeds = new_news_feeds
        self.save_news_feeds()

    def load_playlists(self):
        _, playlists_file, _, _ = default_files()
        playlists_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.playlists = json.loads(playlists_file.read_text(encoding="utf-8"))
        except Exception:
            self.playlists = {}
        self._refresh_playlists_list()

    def _refresh_playlists_list(self):
        w = self.list_manager.playlists_list_widget
        w.clear()
        for name, url in self.playlists.items():
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, url)
            w.addItem(item)

    def save_playlists(self):
        _, f, _, _ = default_files()
        f.parent.mkdir(parents=True, exist_ok=True)
        try:
            f.write_text(json.dumps(self.playlists, indent=4), encoding="utf-8")
        except Exception:
            pass

    def add_playlist_url(self):
        dialog = FeedDialog(self, name="", url="")
        dialog.setWindowTitle(tr("Playlist", "Add Playlist URL"))
        dialog.set_name_label(tr("Playlist", "Name"))
        if dialog.exec():
            name, url = dialog.get_feed_data()
            name = name.strip()
            url = url.strip()
            if name and url:
                self.playlists[name] = url
                self.save_playlists()
                self._refresh_playlists_list()

    def import_playlist_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Playlist", "Import playlist file"),
            "",
            "M3U (*.m3u *.m3u8);;All files (*.*)",
        )
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            QMessageBox.warning(self, APP_NAME, tr("Playlist", "Could not read playlist file"))
            return

        channels = parse_m3u(content)
        if not channels:
            QMessageBox.information(self, APP_NAME, tr("Playlist", "No channels found in this playlist"))
            return

        dialog = PlaylistViewerDialog(self, channels)
        if dialog.exec():
            selected_channels = dialog.get_selected_channels()
            for name, url in selected_channels:
                self._enqueue(url, name)

    def export_grid_as_m3u(self):
        if not self.video_widgets:
            QMessageBox.information(self, APP_NAME, tr("Playlist", "There are no videos in the grid to export"))
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Playlist", "Export grid as M3U"),
            "",
            "M3U (*.m3u);;All files (*.*)",
        )
        if not path:
            return
        lines = ["#EXTM3U"]
        for tile in self.video_widgets:
            lines.append(f"#EXTINF:-1,{tile.title}")
            lines.append(tile.url)
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            QMessageBox.warning(self, APP_NAME, tr("Playlist", "Could not save playlist"))

    def add_playlist_to_grid(self):
        selected = self.list_manager.playlists_list_widget.selectedItems()
        if not selected:
            return
        playlist_url = selected[0].data(Qt.ItemDataRole.UserRole)
        text = self._fetch_text(playlist_url)
        if text is None:
            QMessageBox.warning(self, "Error", "Could not fetch playlist.")
            return
        self._enqueue_playlist_channels(text, choose=True)

    def add_all_playlist_channels(self):
        selected = self.list_manager.playlists_list_widget.selectedItems()
        if not selected:
            return
        for item in selected:
            playlist_url = item.data(Qt.ItemDataRole.UserRole)
            text = self._fetch_text(playlist_url)
            if text is None:
                QMessageBox.warning(self, "Error", "Could not fetch playlist.")
                continue
            self._enqueue_playlist_channels(text, choose=False)

    def view_playlist_channels(self):
        selected = self.list_manager.playlists_list_widget.selectedItems()
        if not selected:
            return
        playlist_url = selected[0].data(Qt.ItemDataRole.UserRole)
        text = self._fetch_text(playlist_url)
        if text is None:
            QMessageBox.warning(self, "Error", "Could not fetch playlist.")
            return
        self._enqueue_playlist_channels(text, choose=True)

    def remove_playlist(self):
        selected = self.list_manager.playlists_list_widget.selectedItems()
        if not selected:
            return
        for item in selected:
            name = item.text()
            self.playlists.pop(name, None)
        self.save_playlists()
        self._refresh_playlists_list()

    def remove_all_playlists(self):
        if not self.playlists:
            return
        reply = QMessageBox.question(
            self,
            tr("List", "Confirm Clear All"),
            tr("List", "Are you sure you want to clear all playlists?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.No:
            return
        self.playlists.clear()
        self.save_playlists()
        self._refresh_playlists_list()

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
        vw = QtTile(url, title, self.settings, parent=self.central_widget_container, muted=not is_first_video)
        vw.requestToggle.connect(self.toggle_mute_single)
        vw.started.connect(self.on_tile_playing)
        vw.requestFullscreen.connect(self.toggle_fullscreen_tile)
        vw.requestPip.connect(self.toggle_pip)
        vw.requestRemove.connect(self.remove_video_widget)
        vw.requestActive.connect(self.activate_tile_audio)
        vw.titleChanged.connect(self.on_tile_title_changed)

        self.video_widgets.append(vw)

        if is_first_video:
            self.currently_unmuted = vw
            self.allow_auto_select = True

        self.update_grid()
        self._enforce_audio_policy_with_retries()

    def remove_video_from_grid_list(self):
        items = self.list_manager.grid_list_widget.selectedItems()
        if not items:
            return
        for item in items:
            w = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(w, QtTile):
                self.remove_video_widget(w)

    def remove_video_widget(self, widget):
        if self._is_clearing:
            return
        if self.fullscreen_tile is widget and self.isFullScreen():
            self.exit_fullscreen()
        if widget in self.video_widgets:
            was_unmuted = widget == self.currently_unmuted
            if was_unmuted:
                self.currently_unmuted = None

            safe_disconnect(widget.requestToggle)
            safe_disconnect(widget.started)
            safe_disconnect(widget.requestFullscreen)
            safe_disconnect(widget.requestPip)
            safe_disconnect(widget.requestRemove)
            safe_disconnect(widget.requestActive)
            safe_disconnect(widget.titleChanged)

            try:
                widget.stop()
            except Exception:
                pass
            try:
                self.video_widgets.remove(widget)
            except Exception:
                pass
            widget.setParent(None)
            widget.deleteLater()
            if was_unmuted and self.video_widgets:
                self.currently_unmuted = self.video_widgets[0]
                self.allow_auto_select = True
            self.update_grid()
            self._enforce_audio_policy_with_retries()

    def remove_all_videos(self):
        if not self.video_widgets:
            return

        reply = QMessageBox.question(
            self,
            tr("UI", "Confirm Remove All"),
            tr("UI", "Are you sure you want to remove all videos from the grid?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.No:
            return

        if self.isFullScreen():
            self.exit_fullscreen()

        self._is_clearing = True

        try:
            self._queue_timer.stop()
        except Exception:
            pass
        self._queue.clear()

        self.currently_unmuted = None

        for w in self.video_widgets[:]:
            safe_disconnect(w.requestToggle)
            safe_disconnect(w.started)
            safe_disconnect(w.requestFullscreen)
            safe_disconnect(w.requestPip)
            safe_disconnect(w.requestRemove)
            safe_disconnect(w.requestActive)
            safe_disconnect(w.titleChanged)
            try:
                w.stop()
            except Exception:
                pass
            w.setParent(None)
            w.deleteLater()

        self.video_widgets.clear()

        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            wid = item.widget()
            if wid is not None:
                wid.setParent(None)

        self.list_manager.grid_list_widget.clear()

        self._audio_enforce_generation += 1

        QTimer.singleShot(0, lambda: setattr(self, "_is_clearing", False))

    def update_grid(self):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            _widget = item.widget()

        active_widgets = [w for w in self.video_widgets if w not in self.pip_windows]
        n_active = len(active_widgets)

        if n_active == 0:
            self.list_manager.grid_list_widget.clear()
            return

        mode = getattr(self.settings, "layout_mode", "auto")
        if mode == "2x2":
            cols = 2
            rows = math.ceil(n_active / cols)
        elif mode == "3x3":
            cols = 3
            rows = math.ceil(n_active / cols)
        elif mode == "1xN":
            cols = n_active
            rows = 1
        elif mode == "Nx1":
            cols = 1
            rows = n_active
        else:
            cols = max(1, math.isqrt(n_active))
            rows = math.ceil(n_active / cols)

        for i in range(self.grid_layout.columnCount()):
            self.grid_layout.setColumnStretch(i, 0)
        for i in range(self.grid_layout.rowCount()):
            self.grid_layout.setRowStretch(i, 0)

        for i in range(cols):
            self.grid_layout.setColumnStretch(i, 1)
        for i in range(rows):
            self.grid_layout.setRowStretch(i, 1)

        for i, w in enumerate(active_widgets):
            row = i // cols
            col = i % cols
            self.grid_layout.addWidget(w, row, col)
            w.show()

        self.list_manager.grid_list_widget.clear()
        for w in self.video_widgets:
            item = QListWidgetItem(w.title)
            item.setData(Qt.ItemDataRole.UserRole, w)
            self.list_manager.grid_list_widget.addItem(item)

    def toggle_mute_single(self, video_widget: QtTile):
        if self._is_clearing:
            return
        if self.settings.audio_policy == "mixed":
            video_widget.set_mute_state(not video_widget.is_muted)
            return

        if self.currently_unmuted == video_widget:
            video_widget.set_mute_state(True)
            self.currently_unmuted = None
            self.allow_auto_select = False
        else:
            self.currently_unmuted = video_widget
            self.allow_auto_select = True
        self._enforce_audio_policy_with_retries()

    def activate_tile_audio(self, video_widget: QtTile):
        if self._is_clearing:
            return
        
        if self.settings.audio_policy == "mixed":
            if video_widget.is_muted:
                video_widget.set_mute_state(False)
            return

        if self.currently_unmuted != video_widget:
            self.currently_unmuted = video_widget
            self.allow_auto_select = True
            self._enforce_audio_policy_with_retries()

    def on_tile_playing(self, tile: QtTile):
        if self._is_clearing:
            return
        if self.currently_unmuted is None and self.allow_auto_select and self.settings.audio_policy == "single":
            self.currently_unmuted = tile
        self._enforce_audio_policy_with_retries()

    def on_tile_title_changed(self, new_title):
        if self.sender() == self.currently_unmuted:
            self.status_label.setText(tr("UI", f"Active: {new_title}"))

    def _update_active_tile_styles(self):
        for tile in self.video_widgets:
            tile._apply_tile_styles()
            if tile is self.currently_unmuted and self.settings.audio_policy == "single":
                tile.setStyleSheet(tile.styleSheet() + "QFrame#tile { border: 3px solid #2a82da; }")

    def _enforce_audio_policy(self, generation: int):
        if generation != self._audio_enforce_generation:
            return
        if self.settings.audio_policy == "mixed":
            self.status_label.setText(tr("UI", "Mixed Audio Mode"))
            self._update_active_tile_styles()
            return
        active = self.currently_unmuted
        for tile in self.video_widgets:
            if tile is not active:
                tile.set_mute_state(True)
        if active:
            active.make_active(self.active_volume)
            self.status_label.setText(tr("UI", f"Active: {active.title}"))
        else:
            self.status_label.setText(tr("UI", "No active audio"))
        self._update_active_tile_styles()

    def _enforce_audio_policy_with_retries(self):
        self._audio_enforce_generation += 1
        gen = self._audio_enforce_generation
        self._enforce_audio_policy(gen)
        for i in range(1, self.AUDIO_RETRY_COUNT + 1):
            QTimer.singleShot(self.AUDIO_RETRY_DELAY_MS * i, lambda g=gen: self._enforce_audio_policy(g))

    def mute_all_tiles(self):
        self.currently_unmuted = None
        self.allow_auto_select = False
        self._enforce_audio_policy_with_retries()

    def cycle_active_audio(self):
        if not self.video_widgets:
            return
        if self.currently_unmuted not in self.video_widgets:
            self.currently_unmuted = self.video_widgets[0]
        else:
            idx = self.video_widgets.index(self.currently_unmuted)
            idx = (idx + 1) % len(self.video_widgets)
            self.currently_unmuted = self.video_widgets[idx]
        self.allow_auto_select = True
        self._enforce_audio_policy_with_retries()

    def on_volume_changed(self, value: int):
        self.active_volume = int(value)
        self.volume_label.setText(tr("UI", f"Volume {self.active_volume}"))
        self._enforce_audio_policy_with_retries()

    def toggle_fullscreen_tile(self, tile: QtTile):
        if self.isFullScreen():
            current_fs_tile = self.fullscreen_tile
            self.exit_fullscreen()
            if current_fs_tile is not tile:
                QTimer.singleShot(50, lambda: self.toggle_fullscreen_tile(tile))
        else:
            self.window_state_before_fullscreen = {
                "geometry": self.saveGeometry(),
                "is_maximized": self.isMaximized(),
                "list_manager_visible": self.list_manager.isVisible(),
            }
            self.fullscreen_tile = tile
            tile.set_is_fullscreen(True)

            if self.main_toolbar:
                self.main_toolbar.hide()
            self.list_manager.hide()

            for w in self.video_widgets:
                if w is not tile and self.settings.pause_others_in_fullscreen:
                    try:
                        w.player.pause()
                    except Exception:
                        pass

            self.takeCentralWidget()
            self.setCentralWidget(tile)
            self.showFullScreen()

    def exit_fullscreen(self):
        if not self.isFullScreen():
            return

        fs_tile = self.fullscreen_tile
        was_tile_fullscreen = fs_tile is not None

        if was_tile_fullscreen:
            tile = self.takeCentralWidget()
            self.setCentralWidget(self.central_widget_container)
            if tile:
                tile.setParent(self.central_widget_container)
            fs_tile.set_is_fullscreen(False)
            self.fullscreen_tile = None

        self.showNormal()

        if self.window_state_before_fullscreen:
            self.list_manager.setVisible(self.window_state_before_fullscreen.get("list_manager_visible", True))
            if self.window_state_before_fullscreen.get("is_maximized", True):
                self.showMaximized()
            else:
                geom = self.window_state_before_fullscreen.get("geometry")
                if geom:
                    self.restoreGeometry(geom)
            self.window_state_before_fullscreen = None

        if self.main_toolbar:
            self.main_toolbar.show()

        for w in self.video_widgets:
            w.show()
            if self.settings.pause_others_in_fullscreen and w is not fs_tile:
                try:
                    w.player.play()
                except Exception:
                    pass

        self.update_grid()
        self.fullscreen_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton))

    def toggle_first_fullscreen(self):
        if self.video_widgets:
            self.toggle_fullscreen_tile(self.video_widgets[0])

    def toggle_grid_fullscreen(self):
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self.window_state_before_fullscreen = {
                "geometry": self.saveGeometry(),
                "is_maximized": self.isMaximized(),
                "list_manager_visible": self.list_manager.isVisible(),
            }
            self.manage_lists_button.hide()
            self.url_action.setVisible(False)
            self.add_video_button.hide()
            self.volume_label.hide()
            self.volume_slider.hide()
            self.list_manager.hide()
            self.showFullScreen()
            self.fullscreen_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton))

    def toggle_pip(self, tile: QtTile):
        if tile in self.pip_windows:
            pip_window = self.pip_windows.pop(tile)
            pip_window.close()
        else:
            pip_window = PipWindow(tile, self)
            self.pip_windows[tile] = pip_window
            self.update_grid()

    def reattach_tile(self, tile: QtTile):
        if tile in self.pip_windows:
            self.pip_windows.pop(tile)
        tile.setParent(self.central_widget_container)
        tile.show()
        self.update_grid()

    def reorder_video_widgets(self, *_):
        new_order_widgets = []
        w = self.list_manager.grid_list_widget
        for i in range(w.count()):
            item = w.item(i)
            video_widget = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(video_widget, QtTile):
                new_order_widgets.append(video_widget)

        pip_widgets = [vw for vw in self.video_widgets if vw not in new_order_widgets]
        self.video_widgets = new_order_widgets + pip_widgets
        self.update_grid()

    def reload_all_videos(self):
        for widget in self.video_widgets:
            widget.safe_reload()

    def save_state(self):
        _, _, state_file, _ = default_files()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "videos": [(w.url, w.title) for w in self.video_widgets],
            "active_volume": int(self.active_volume),
            "geometry": self.saveGeometry().toBase64().data().decode("utf-8"),
            "is_maximized": self.isMaximized(),
        }
        try:
            state_file.write_text(json.dumps(state, indent=4), encoding="utf-8")
        except Exception:
            pass

    def load_state(self):
        _, _, state_file, _ = default_files()
        if not state_file.exists():
            self.showMaximized()
            return

        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.active_volume = int(state.get("active_volume", self.settings.volume_default))

            geometry_b64 = state.get("geometry")
            if geometry_b64:
                self.restoreGeometry(QByteArray.fromBase64(geometry_b64.encode("utf-8")))

            if state.get("is_maximized", True):
                self.showMaximized()
            else:
                self.showNormal()

            for url, title in state.get("videos", []):
                self._enqueue(url, title)
        except Exception:
            self.showMaximized()

    def closeEvent(self, event):
        self._is_clearing = True
        try:
            self._queue_timer.stop()
        except Exception:
            pass
        try:
            for w in self.video_widgets[:]:
                w.stop()
        except Exception:
            pass
        self.save_state()
        super().closeEvent(event)

    def open_settings(self):
        dlg = SettingsDialog(self, self.sm)
        result = dlg.exec()

        if dlg.profile_imported:
            self.refresh_ui()
            QMessageBox.information(self, APP_NAME, tr("Settings", "Profile imported and applied."))
        elif result:
            dlg.apply()
            self.settings = self.sm.settings
            self.apply_theme()
            QMessageBox.information(self, APP_NAME, tr("Settings", "Settings saved"))

    def open_diagnostics(self):
        dlg = DiagnosticsDialog(self, self.sm)
        dlg.exec()

    def open_help_overlay(self):
        text = (
            tr("Help", "Shortcuts:\n")
            + "F: "
            + tr("Help", "Toggle fullscreen for first tile")
            + "\n"
            + "Ctrl+Shift+A: "
            + tr("Help", "Add all feeds")
            + "\n"
            + "Delete: "
            + tr("Help", "Remove selected from grid list")
            + "\n"
        )
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Help", "Quick help"))
        layout = QVBoxLayout(dlg)
        label = QLabel(text)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        layout.addWidget(btns)
        dlg.exec()

    def open_about(self):
        msg = QMessageBox(self)
        msg.setWindowTitle(tr("About", "About"))
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            f"{APP_NAME} {APP_VERSION}\n"
            f"{tr('About', 'A lightweight grid for live video with single active audio')}\n\n"
            f"{tr('About', 'Third party content plays under the respective site terms')}\n"
            f"{tr('About', 'You are responsible for how you use streams and embeds')}"
        )
        msg.exec()


def main():
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName(APP_ORG)
    QCoreApplication.setOrganizationDomain(APP_DOMAIN)
    QCoreApplication.setApplicationVersion(APP_VERSION)

    migrate_legacy_settings()

    _, _, _, log_file_path = default_files()
    logger = setup_logging(log_file_path)

    def qt_message_handler(msg_type, _msg_log_context, msg_string):
        try:
            logger.info("%s: %s", msg_type.name, msg_string)
        except Exception:
            pass

    qInstallMessageHandler(qt_message_handler)

    app = QApplication(sys.argv)
    app.setApplicationDisplayName(APP_NAME)

    sm = SettingsManager(logger)
    sm.load()

    win = NewsBoard(sm)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
