"""
NewsBoard
Cross platform PyQt6 video grid with single active audio and polished UX

Implements:
1 Cross platform foundations via QStandardPaths with legacy settings migration
2 Media playback primary on Qt Multimedia with a tiny capability probe
4 Polished UX: help overlay, status toasts, better fullscreen behavior
6 Performance: staggered starts, pause others during fullscreen, idle hints
7 Settings: export and import, per host preferred backend stub, audio policy
8 Accessibility: focus rings, accessible names, shortcuts, translatable strings
10 Content and legal: About dialog with third party notice
11 Concrete tweaks: safe reload, robust Remove All, defensive disconnects

Tested against PyQt6 6.6 plus. QWebEngine is optional and not required here.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import threading
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlparse
import requests

from PyQt6.QtCore import (
    QByteArray,
    QCoreApplication,
    QEvent,
    QLocale,
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
    QGuiApplication,
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
    QGroupBox,
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
)

from PyQt6.QtMultimedia import (
    QAudioOutput,
    QMediaPlayer,
)
from PyQt6.QtMultimediaWidgets import QVideoWidget

# Optional dependency
try:
    from yt_dlp import YoutubeDL

    YT_AVAILABLE = True
except Exception:
    YT_AVAILABLE = False

APP_NAME = "NewsBoard"
APP_ORG = "Farleyman.com"
APP_DOMAIN = "Farleyman.com"
APP_VERSION = "25.11.12"

# ---------------- i18n helpers ----------------


def tr(ctx: str, text: str) -> str:
    return QCoreApplication.translate(ctx, text)


# ---------------- Cross platform storage ----------------


def user_app_dir() -> Path:
    loc = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    base = Path(loc) if loc else Path.home() / f".{APP_NAME.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_files() -> Tuple[Path, Path, Path, Path]:
    base = user_app_dir()
    settings_dir = base
    feeds = settings_dir / "news_feeds.json"
    playlists = settings_dir / "playlists.json"
    state = settings_dir / "app_state.json"
    log = settings_dir / "newsboard.log"
    return feeds, playlists, state, log


def migrate_legacy_settings() -> None:
    legacy = Path("resources") / "settings"
    feeds, playlists, state, log = default_files()
    try:
        if legacy.exists() and legacy.is_dir():
            moved = False
            src_feeds = legacy / "news_feeds.json"
            src_state = legacy / "app_state.json"
            if src_feeds.exists() and not feeds.exists():
                feeds.write_bytes(src_feeds.read_bytes())
                moved = True
            if src_state.exists() and not state.exists():
                state.write_bytes(src_state.read_bytes())
                moved = True
            if moved:
                # keep legacy folder, just inform via toast later
                pass
    except Exception:
        pass


# ---------------- URL helpers ----------------

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
    """Parses M3U content and extracts channel information."""
    channels = []
    lines = [line.strip() for line in content.splitlines()]

    if not lines or not lines[0].startswith("#EXTM3U"):
        # If it's not a valid M3U, maybe it's just a list of URLs
        return [("Pasted Link", line) for line in lines if line and not line.startswith("#")]

    i = 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF:"):
            info_line = lines[i]
            # The URL is expected to be the next line that is not a comment/tag
            url_line = ""
            for j in range(i + 1, len(lines)):
                if lines[j] and not lines[j].startswith("#"):
                    url_line = lines[j]
                    i = j # Skip to the line after the URL
                    break
            
            if url_line:
                # Extract name from the end of the EXTINF line
                name_match = re.search(r',(.+)$', info_line)
                name = name_match.group(1) if name_match else "Unnamed Channel"
                
                # Also try to get a better name from tvg-name attribute
                tvg_name_match = re.search(r'tvg-name="([^"]+)"', info_line)
                if tvg_name_match:
                    name = tvg_name_match.group(1)

                channels.append((name.strip(), url_line.strip()))
        i += 1
        
    return channels


# ---------------- Settings model ----------------


@dataclass
class AppSettings:
    audio_policy: str = "single"  # single or mixed
    volume_default: int = 85
    yt_mode: str = "direct_when_possible"  # embed_only or direct_when_possible
    per_host_preferred_backend: Dict[str, str] = None  # host to "qt"
    privacy_embed_only_youtube: bool = False
    pause_others_in_fullscreen: bool = True
    theme: str = "system"

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
        return s


class SettingsManager:
    def __init__(self):
        feeds, playlists, state, log = default_files()
        self.feeds_file = feeds
        self.playlists_file = playlists
        self.state_file = state
        self.log_file = log
        self.settings_file = user_app_dir() / "settings.json"
        self.settings = AppSettings()

    def load(self):
        try:
            if self.settings_file.exists():
                d = json.loads(self.settings_file.read_text(encoding="utf-8"))
                self.settings = AppSettings.from_dict(d)
        except Exception:
            pass

    def save(self):
        try:
            self.settings_file.write_text(self.settings.to_json(), encoding="utf-8")
        except Exception:
            pass

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
        Path(path).write_text(json.dumps(bundle, indent=4), encoding="utf-8")

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
            except Exception as e:
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


# ---------------- Capability probe and backend choice ----------------


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
    # crude probe based on common mime presence
    try:
        mimes = QMediaPlayer.supportedMimeTypes()
        return any("application/vnd.apple.mpegurl" in m for m in mimes) or any("vnd.apple.mpegurl" in m for m in mimes)
    except Exception:
        return False


def choose_backend(url: str, settings: AppSettings) -> str:
    host = urlparse(url).netloc.lower()
    if host in (settings.per_host_preferred_backend or {}):
        return settings.per_host_preferred_backend[host]
    if is_youtube_url(url):
        if settings.privacy_embed_only_youtube:
            return "embed"  # not implemented here, reserved
        return "qt"
    return "qt"


# ---------------- Async YouTube resolver ----------------


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


# ---------------- Tile widget ----------------


class QtTile(QFrame):
    requestToggle = pyqtSignal(object)
    started = pyqtSignal(object)
    requestFullscreen = pyqtSignal(object)
    requestRemove = pyqtSignal(object)
    requestPip = pyqtSignal(object)

    def __init__(self, url: str, title: str, settings: AppSettings, parent=None, muted: bool = True):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.url = url
        self.title = title or tr("Tile", "Video")
        self.is_muted = muted
        self.is_fullscreen_tile = False
        self.is_pip_tile = False
        self._settings = settings
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.video_widget = QVideoWidget(self)
        self.video_widget.setObjectName("video")
        self.video_widget.setMinimumSize(200, 112)
        self.video_widget.setAccessibleName(tr("Tile", "Video surface"))
        outer.addWidget(self.video_widget, 1)

        controls = QWidget(self)
        controls.setObjectName("controls")
        controls.setAccessibleName(tr("Tile", "Controls"))
        row = QHBoxLayout(controls)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(6)

        self.label = QLabel(self.title, controls)
        self.label.setAccessibleName(tr("Tile", "Title"))
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.mute_button = QPushButton("", controls)
        self.mute_button.setAccessibleName(tr("Tile", "Mute"))
        self.mute_button.setFixedSize(24, 24)

        self.reload_button = QPushButton("", controls)
        self.reload_button.setAccessibleName(tr("Tile", "Reload"))
        self.reload_button.setFixedSize(24, 24)

        self.pip_button = QPushButton("", controls)
        self.pip_button.setAccessibleName(tr("Tile", "Picture-in-Picture"))
        self.pip_button.setFixedSize(24, 24)

        self.fullscreen_button = QPushButton("", controls)
        self.fullscreen_button.setAccessibleName(tr("Tile", "Fullscreen"))
        self.fullscreen_button.setFixedSize(24, 24)

        self.remove_button = QPushButton("", controls)
        self.remove_button.setAccessibleName(tr("Tile", "Remove"))
        self.remove_button.setFixedSize(24, 24)

        row.addWidget(self.label)
        row.addStretch()
        row.addWidget(self.mute_button)
        row.addWidget(self.reload_button)
        row.addWidget(self.pip_button)
        row.addWidget(self.fullscreen_button)
        row.addWidget(self.remove_button)
        outer.addWidget(controls, 0)

        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget.videoSink())

        self.audio.setMuted(self.is_muted)
        self.audio.setVolume(0.0 if self.is_muted else float(self._settings.volume_default) / 100.0)

        self.player.playbackStateChanged.connect(lambda *_: self.started.emit(self))
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)

        self._refresh_icons()

        self.mute_button.clicked.connect(lambda: self.requestToggle.emit(self))
        self.reload_button.clicked.connect(self.safe_reload)
        self.pip_button.clicked.connect(lambda: self.requestPip.emit(self))
        self.fullscreen_button.clicked.connect(lambda: self.requestFullscreen.emit(self))
        self.remove_button.clicked.connect(lambda: self.requestRemove.emit(self))

        self.play_url(self.url)

    def contextMenuEvent(self, event: QEvent):
        menu = QMenu(self)
        style = self.style()

        # Mute/Unmute Action
        mute_text = tr("Tile", "Unmute") if self.is_muted else tr("Tile", "Mute")
        mute_icon = style.standardIcon(QStyle.StandardPixmap.SP_MediaVolume if self.is_muted else QStyle.StandardPixmap.SP_MediaVolumeMuted)
        mute_action = QAction(mute_icon, mute_text, self)
        mute_action.triggered.connect(lambda: self.requestToggle.emit(self))
        menu.addAction(mute_action)

        # Picture-in-Picture Action
        pip_text = tr("Tile", "Exit Picture-in-Picture") if self.is_pip_tile else tr("Tile", "Enter Picture-in-Picture")
        pip_icon = style.standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton if self.is_pip_tile else QStyle.StandardPixmap.SP_TitleBarMinButton)
        pip_action = QAction(pip_icon, pip_text, self)
        pip_action.triggered.connect(lambda: self.requestPip.emit(self))
        menu.addAction(pip_action)

        # Fullscreen Action
        fullscreen_text = tr("Tile", "Exit Fullscreen") if self.is_fullscreen_tile else tr("Tile", "Enter Fullscreen")
        fullscreen_icon = style.standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton if self.is_fullscreen_tile else QStyle.StandardPixmap.SP_TitleBarMaxButton)
        fullscreen_action = QAction(fullscreen_icon, fullscreen_text, self)
        fullscreen_action.triggered.connect(lambda: self.requestFullscreen.emit(self))
        menu.addAction(fullscreen_action)

        # Reload Action
        reload_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload), tr("Tile", "Reload"), self)
        reload_action.triggered.connect(self.safe_reload)
        menu.addAction(reload_action)

        # Copy URL Action
        copy_url_action = QAction(tr("Tile", "Copy URL"), self)
        copy_url_action.triggered.connect(self.copy_url)
        menu.addAction(copy_url_action)

        menu.addSeparator()

        # Remove Action
        remove_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), tr("Tile", "Remove"), self)
        remove_action.triggered.connect(lambda: self.requestRemove.emit(self))
        menu.addAction(remove_action)

        menu.exec(event.globalPos())

    def copy_url(self):
        QApplication.clipboard().setText(self.url)

    def on_media_status_changed(self, status: QMediaPlayer.MediaStatus):
        if status == QMediaPlayer.MediaStatus.LoadingMedia:
            self.label.setText(f"{self.title}  {tr('Tile','Loading')}")
        elif status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.label.setText(self.title)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.label.setText(f"{self.title}  {tr('Tile','Invalid media')}")
        elif status == QMediaPlayer.MediaStatus.StalledMedia:
            self.label.setText(f"{self.title}  {tr('Tile','Stalled')}")
        elif status == QMediaPlayer.MediaStatus.BufferingMedia:
            self.label.setText(f"{self.title}  {tr('Tile','Buffering')}")

    def play_url(self, url: str):
        chosen = choose_backend(url, self._settings)
        src = build_embed_or_watch(url)
        if chosen == "qt" and is_youtube_url(src) and self._settings.yt_mode != "embed_only":
            if YT_AVAILABLE:
                self.label.setText(f"{self.title}  {tr('Tile','Resolving')}")
                self._yt_worker = YtResolveWorker(src, self)
                self._yt_worker.resolved.connect(lambda direct: self._apply_source(QUrl(direct)))
                self._yt_worker.failed.connect(lambda _msg: self.label.setText(f"{self.title}  {tr('Tile','Resolve failed')}"))
                self._yt_worker.start()
                return
            else:
                self.label.setText(f"{self.title}  {tr('Tile','yt dlp not installed')}")
                return
        self._apply_source(QUrl(src))

    def _apply_source(self, qurl: QUrl):
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
        self.mute_button.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaVolumeMuted if self.is_muted
                               else QStyle.StandardPixmap.SP_MediaVolume)
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
        try:
            # drop the old player to recover from backend stalls
            self.player.mediaStatusChanged.disconnect(self.on_media_status_changed)
        except Exception:
            pass
        try:
            self.player.deleteLater()
        except Exception:
            pass
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget.videoSink())
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        if self.is_muted:
            self.audio.setMuted(True)
            self.audio.setVolume(0.0)
        self.play_url(self.url)

    def stop(self):
        # defensive disconnects
        for fn in (
            lambda: self.player.playbackStateChanged.disconnect(),
            lambda: self.player.mediaStatusChanged.disconnect(),
            lambda: self.mute_button.clicked.disconnect(),
            lambda: self.reload_button.clicked.disconnect(),
            lambda: self.pip_button.clicked.disconnect(),
            lambda: self.fullscreen_button.clicked.disconnect(),
            lambda: self.remove_button.clicked.disconnect(),
        ):
            try:
                fn()
            except Exception:
                pass
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

    def closeEvent(self, e):
        self.stop()
        super().closeEvent(e)


# ---------------- Feed dialog and list dock ----------------


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

        # Feeds tab
        self.news_feed_tab = QWidget()
        self.news_feed_layout = QVBoxLayout(self.news_feed_tab)
        self.news_feed_layout.setContentsMargins(6, 6, 6, 6)
        self.news_feed_layout.setSpacing(6)

        self.toolbar = QWidget()
        self.toolbar_layout = QHBoxLayout(self.toolbar)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout.setSpacing(6)

        self.add_feed_to_grid_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton), tr("List", "Add Selected"))
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

        # Playlists tab
        self.playlists_tab = QWidget()
        self.playlists_layout = QVBoxLayout(self.playlists_tab)
        self.playlists_layout.setContentsMargins(6, 6, 6, 6)
        self.playlists_layout.setSpacing(6)

        self.playlists_toolbar = QWidget()
        self.playlists_toolbar_layout = QHBoxLayout(self.playlists_toolbar)
        self.playlists_toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.playlists_toolbar_layout.setSpacing(6)

        self.add_playlist_to_grid_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton), tr("List", "Add Selected"))
        self.add_all_playlist_channels_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward), tr("List", "Add All"))
        self.view_playlist_channels_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_DesktopIcon), tr("List", "View Channels"))
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

        # Grid tab
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


# ---------------- Diagnostics and Settings dialogs ----------------


class DiagnosticsDialog(QDialog):
    def __init__(self, parent, settings: SettingsManager):
        super().__init__(parent)
        self.setWindowTitle(tr("Diag", "Diagnostics"))
        lay = QVBoxLayout(self)

        text = QTextEdit()
        text.setReadOnly(True)
        feeds, playlists, state, log = settings.feeds_file, settings.playlists_file, settings.state_file, settings.log_file

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
        text.setPlainText("\n".join(lines))

        lay.addWidget(text)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
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

        self.populate_fields()

        form.addRow(tr("Settings", "Audio policy"), self.audio_policy)
        form.addRow(tr("Settings", "Default volume"), self.volume_default)
        form.addRow(tr("Settings", "YouTube mode"), self.yt_mode)
        form.addRow(tr("Settings", "Theme"), self.theme)
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
        self._sm.save()


# ---------------- Main window ----------------


class PipWindow(QWidget):
    def __init__(self, tile: QtTile, main_window: QMainWindow):
        super().__init__()
        self.tile = tile
        self.main_window = main_window
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setWindowTitle(f"PiP - {tile.title}")

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
        self.settings = sm.settings
        self.window_state_before_fullscreen = None
        self.pip_windows: Dict[QtTile, PipWindow] = {}

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(QIcon("resources/icon.ico"))
        self.setBaseSize(1280, 720)

        self.apply_theme()

        self.central_widget = QWidget()
        self.central_widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        the_layout = QGridLayout(self.central_widget)
        the_layout.setContentsMargins(0, 0, 0, 0)
        the_layout.setSpacing(4)
        self.grid_layout = the_layout
        self.setCentralWidget(self.central_widget)

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

        # First run checks
        self.first_run_checklist()

        # Focus and keyboard
        self.central_widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def refresh_ui(self):
        self.sm.load()
        self.settings = self.sm.settings
        self.apply_theme()
        self.load_news_feeds()
        self.load_playlists()
        self.remove_all_videos()
        self.load_state()

    # Theming
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
            app.setStyleSheet('''
                QToolTip {
                    color: #ffffff;
                    background-color: #2a82da;
                    border: 1px solid white;
                }
                QMenuBar {
                    background-color: #353535;
                }
                QMenuBar::item {
                    background-color: #353535;
                    color: #ffffff;
                }
                QMenuBar::item:selected {
                    background-color: #2a82da;
                }
                QMenu {
                    background-color: #353535;
                    border: 1px solid #555555;
                }
                QMenu::item {
                    color: #ffffff;
                }
                QMenu::item:selected {
                    background-color: #2a82da;
                }
            ''')
        else:
            app.setStyleSheet("")
            app.setPalette(QApplication.style().standardPalette())

    # Menu bar and actions
    def _build_menus(self):
        mb = self.menuBar()

        file_menu = mb.addMenu(tr("Menu", "File"))
        
        act_add_playlist = QAction(tr("Menu", "Add Playlist URL"), self)
        act_add_playlist.triggered.connect(self.add_playlist_url)
        file_menu.addAction(act_add_playlist)
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
        act_about = QAction(tr("Menu", "About"), self)
        act_about.triggered.connect(self.open_about)
        helpm.addAction(act_about)

    # Toolbar and top controls
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
        self.manage_lists_button.setAccessibleName(tr("UI", "Manage Lists"))
        tb.addWidget(self.manage_lists_button)

        spacer_small = QWidget()
        spacer_small.setFixedWidth(6)
        tb.addWidget(spacer_small)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(tr("UI", "Enter URL or iframe"))
        self.url_input.setToolTip(tr("UI", "Paste a stream URL or iframe"))
        self.url_input.setAccessibleName(tr("UI", "URL input"))
        self.url_action = QWidgetAction(self)
        self.url_action.setDefaultWidget(self.url_input)
        tb.addAction(self.url_action)

        self.add_video_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton),
            tr("UI", "Add Video"),
        )
        self.add_video_button.clicked.connect(self.add_video_from_input)
        self.add_video_button.setAccessibleName(tr("UI", "Add Video"))
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
        self.volume_slider.setAccessibleName(tr("UI", "Volume slider"))

        tb.addWidget(self.volume_label)
        tb.addWidget(self.volume_slider)

        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer2)

        self.fullscreen_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton), tr("UI", "Fullscreen")
        )
        self.fullscreen_button.clicked.connect(self.toggle_grid_fullscreen)
        self.fullscreen_button.setAccessibleName(tr("UI", "Toggle Fullscreen"))
        tb.addWidget(self.fullscreen_button)

        self.reload_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload), tr("UI", "Reload All")
        )
        self.reload_all_button.clicked.connect(self.reload_all_videos)
        self.reload_all_button.setAccessibleName(tr("UI", "Reload all videos"))
        tb.addWidget(self.reload_all_button)

        self.remove_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon), tr("UI", "Remove All")
        )
        self.remove_all_button.clicked.connect(self.remove_all_videos)
        self.remove_all_button.setAccessibleName(tr("UI", "Remove all videos"))
        tb.addWidget(self.remove_all_button)

        # Shortcuts
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

    def import_profile_action(self):
        if self.sm.import_profile(self):
            self.refresh_ui()
            QMessageBox.information(self, APP_NAME, tr("Settings", "Profile imported and applied."))

    # First run media checklist
    def first_run_checklist(self):
        msgs = []
        if platform_name() == "Linux" and not have_gstreamer():
            msgs.append(tr("Diag", "GStreamer is not found. Install base good bad and ugly sets."))
        if msgs:
            QMessageBox.information(self, APP_NAME, "\n".join(msgs))

    # Feeds
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
            feeds_file.write_text(json.dumps(feeds, indent=4), encoding="utf-8")
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

    # Playlists
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
            if name and url:
                self.playlists[name] = url
                self.save_playlists()
                self._refresh_playlists_list()

    def add_playlist_to_grid(self):
        selected = self.list_manager.playlists_list_widget.selectedItems()
        if not selected:
            return

        item = selected[0]
        playlist_url = item.data(Qt.ItemDataRole.UserRole)

        try:
            response = requests.get(playlist_url, timeout=10)
            response.raise_for_status()
            m3u_content = response.text
        except requests.exceptions.RequestException as e:
            QMessageBox.warning(self, "Error", f"Could not fetch playlist: {e}")
            return

        channels = parse_m3u(m3u_content)
        if not channels:
            QMessageBox.information(self, "Playlist", "No channels found in this playlist.")
            return

        dialog = PlaylistViewerDialog(self, channels)
        if dialog.exec():
            selected_channels = dialog.get_selected_channels()
            for name, url in selected_channels:
                self._enqueue(url, name)

    def add_all_playlist_channels(self):
        selected = self.list_manager.playlists_list_widget.selectedItems()
        if not selected:
            return
        for item in selected:
            playlist_url = item.data(Qt.ItemDataRole.UserRole)
            try:
                response = requests.get(playlist_url, timeout=10)
                response.raise_for_status()
                m3u_content = response.text
            except requests.exceptions.RequestException as e:
                QMessageBox.warning(self, "Error", f"Could not fetch playlist: {e}")
                continue

            channels = parse_m3u(m3u_content)
            for name, url in channels:
                self._enqueue(url, name)

    def view_playlist_channels(self):
        selected = self.list_manager.playlists_list_widget.selectedItems()
        if not selected:
            return

        item = selected[0]
        playlist_url = item.data(Qt.ItemDataRole.UserRole)

        try:
            response = requests.get(playlist_url, timeout=10)
            response.raise_for_status()
            m3u_content = response.text
        except requests.exceptions.RequestException as e:
            QMessageBox.warning(self, "Error", f"Could not fetch playlist: {e}")
            return

        channels = parse_m3u(m3u_content)
        if not channels:
            QMessageBox.information(self, "Playlist", "No channels found in this playlist.")
            return

        dialog = PlaylistViewerDialog(self, channels)
        if dialog.exec():
            selected_channels = dialog.get_selected_channels()
            for name, url in selected_channels:
                self._enqueue(url, name)


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

    # Queue and grid
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
        vw = QtTile(url, title, self.settings, parent=self.central_widget, muted=not is_first_video)
        vw.requestToggle.connect(self.toggle_mute_single)
        vw.started.connect(self.on_tile_playing)
        vw.requestFullscreen.connect(self.toggle_fullscreen_tile)
        vw.requestPip.connect(self.toggle_pip)
        vw.requestRemove.connect(self.remove_video_widget)

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
            for fn in (
                lambda: widget.requestToggle.disconnect(),
                lambda: widget.started.disconnect(),
                lambda: widget.requestFullscreen.disconnect(),
                lambda: widget.requestPip.disconnect(),
                lambda: widget.requestRemove.disconnect(),
            ):
                try:
                    fn()
                except Exception:
                    pass
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
            for fn in (
                lambda: w.requestToggle.disconnect(),
                lambda: w.started.disconnect(),
                lambda: w.requestFullscreen.disconnect(),
                lambda: w.requestPip.disconnect(),
                lambda: w.requestRemove.disconnect(),
            ):
                try:
                    fn()
                except Exception:
                    pass
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
        # Clear the grid layout completely.
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)

        active_widgets = [w for w in self.video_widgets if w not in self.pip_windows]
        n_active = len(active_widgets)

        if n_active == 0:
            self.list_manager.grid_list_widget.clear()
            return

        cols = max(1, math.isqrt(n_active))
        rows = math.ceil(n_active / cols)

        # Set column and row stretch to make them equally sized
        for i in range(self.grid_layout.columnCount()):
            self.grid_layout.setColumnStretch(i, 0) # Reset first
        for i in range(self.grid_layout.rowCount()):
            self.grid_layout.setRowStretch(i, 0) # Reset first

        for i in range(cols):
            self.grid_layout.setColumnStretch(i, 1)
        for i in range(rows):
            self.grid_layout.setRowStretch(i, 1)

        # Re-add non-PiP widgets to the grid
        for i, w in enumerate(active_widgets):
            row = i // cols
            col = i % cols
            self.grid_layout.addWidget(w, row, col)

        # Update the list widget
        self.list_manager.grid_list_widget.clear()
        for w in self.video_widgets:
            item = QListWidgetItem(w.title)
            item.setData(Qt.ItemDataRole.UserRole, w)
            self.list_manager.grid_list_widget.addItem(item)

    # Audio policy
    def toggle_mute_single(self, video_widget: QtTile):
        if self._is_clearing:
            return
        if self.settings.audio_policy == "mixed":
            # simple toggle only for the clicked tile
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

    def on_tile_playing(self, tile: QtTile):
        if self._is_clearing:
            return
        if self.currently_unmuted is None and self.allow_auto_select and self.settings.audio_policy == "single":
            self.currently_unmuted = tile
        self._enforce_audio_policy_with_retries()

    def _enforce_audio_policy(self, generation: int):
        if generation != self._audio_enforce_generation:
            return
        if self.settings.audio_policy == "mixed":
            return
        active = self.currently_unmuted
        for tile in self.video_widgets:
            if tile is not active:
                tile.set_mute_state(True)
        if active:
            active.make_active(self.active_volume)

    def _enforce_audio_policy_with_retries(self):
        self._audio_enforce_generation += 1
        gen = self._audio_enforce_generation
        self._enforce_audio_policy(gen)
        for i in range(1, self.AUDIO_RETRY_COUNT + 1):
            QTimer.singleShot(self.AUDIO_RETRY_DELAY_MS * i, lambda g=gen: self._enforce_audio_policy(g))

    # Volume UI
    def on_volume_changed(self, value: int):
        self.active_volume = int(value)
        self.volume_label.setText(tr("UI", f"Volume {self.active_volume}"))
        self._enforce_audio_policy_with_retries()

    # Fullscreen
    def toggle_fullscreen_tile(self, tile: QtTile):
        if self.isFullScreen():
            if self.fullscreen_tile == tile:
                self.exit_fullscreen()
            else:
                if self.fullscreen_tile:
                    self.fullscreen_tile.set_is_fullscreen(False)
                for w in self.video_widgets:
                    w.setVisible(w is tile)
                self.fullscreen_tile = tile
                self.fullscreen_tile.set_is_fullscreen(True)
        else:
            self.window_state_before_fullscreen = {
                "geometry": self.saveGeometry(),
                "is_maximized": self.isMaximized(),
                "list_manager_visible": self.list_manager.isVisible()
            }
            self.fullscreen_tile = tile
            self.fullscreen_tile.set_is_fullscreen(True)
            if self.main_toolbar:
                self.main_toolbar.hide()
            self.list_manager.hide()
            for w in self.video_widgets:
                w.setVisible(w is tile)
                if self.settings.pause_others_in_fullscreen and w is not tile:
                    try:
                        w.player.pause()
                    except Exception:
                        pass
            self.showFullScreen()

    def exit_fullscreen(self):
        was_tile_fullscreen = self.fullscreen_tile is not None
        fs = self.fullscreen_tile

        try:
            if self.fullscreen_tile:
                self.fullscreen_tile.set_is_fullscreen(False)
        except Exception:
            pass
        self.fullscreen_tile = None

        if was_tile_fullscreen:
            if self.main_toolbar:
                self.main_toolbar.show()
        else:  # was grid fullscreen
            self.manage_lists_button.show()
            self.url_action.setVisible(True)
            self.add_video_button.show()
            self.volume_label.show()
            self.volume_slider.show()

        for w in self.video_widgets:
            w.show()
            if self.settings.pause_others_in_fullscreen and w is not fs:
                try:
                    w.player.play()
                except Exception:
                    pass

        self.showNormal()  # Exit fullscreen first

        if self.window_state_before_fullscreen:
            self.list_manager.setVisible(self.window_state_before_fullscreen["list_manager_visible"])
            if self.window_state_before_fullscreen["is_maximized"]:
                self.showMaximized()
            else:
                self.restoreGeometry(self.window_state_before_fullscreen["geometry"])
            self.window_state_before_fullscreen = None

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
                "list_manager_visible": self.list_manager.isVisible()
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

        tile.setParent(self.central_widget)
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

    # State
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

            # Stagger start for performance
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

    # Dialogs
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

    def open_about(self):
        msg = QMessageBox(self)
        msg.setWindowTitle(tr("About", "About"))
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            f"{APP_NAME} {APP_VERSION}\n"
            f"{tr('About','A lightweight grid for live video with single active audio')}\n\n"
            f"{tr('About','Third party content plays under the respective site terms')}\n"
            f"{tr('About','You are responsible for how you use streams and embeds')}"
        )
        msg.exec()


# ---------------- Entry point ----------------


def main():
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName(APP_ORG)
    QCoreApplication.setOrganizationDomain(APP_DOMAIN)
    QCoreApplication.setApplicationVersion(APP_VERSION)

    def silent_handler(msg_type, msg_log_context, msg_string):
        pass

    qInstallMessageHandler(silent_handler)

    app = QApplication(sys.argv)
    app.setApplicationDisplayName(APP_NAME)

    migrate_legacy_settings()

    sm = SettingsManager()
    sm.load()

    win = NewsBoard(sm)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
