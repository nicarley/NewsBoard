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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlparse

import requests
from PyQt6.QtCore import (
    QByteArray,
    QCoreApplication,
    QEvent,
    QMimeData,
    QTimer,
    QUrl,
    Qt,
    QStandardPaths,
    QSize,
    QObject,
    pyqtSignal,
    qInstallMessageHandler,
    QRectF
)
from PyQt6.QtGui import (
    QAction,
    QDrag,
    QIcon,
    QKeySequence,
    QPalette,
    QColor,
)
from PyQt6.QtMultimedia import (
    QAudioOutput,
    QMediaPlayer,
)
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
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
    QGraphicsView,
    QGraphicsScene,
    QListWidgetItem,
)

try:
    from yt_dlp import YoutubeDL

    YT_AVAILABLE = True
except Exception:
    YT_AVAILABLE = False


APP_NAME = "NewsBoard"
APP_ORG = "Farleyman.com"
APP_DOMAIN = "Farleyman.com"
APP_VERSION = "26.03.17"
LAYOUT_MODES = ["auto", "2x2", "3x3", "4x4", "1xN", "Nx1"]


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
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler(log_path, encoding="utf-8", delay=True)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def configure_logging(logger: logging.Logger, verbose: bool) -> None:
    level = logging.INFO if verbose else logging.WARNING
    logger.setLevel(level)
    for handler in logger.handlers:
        handler.setLevel(level)


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
        "format": "bestvideo*+bestaudio/best",
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


def default_feed_profile(url: str) -> Dict[str, Any]:
    return {
        "url": url,
        "start_muted": True,
        "preferred_volume": 85,
        "preferred_backend": "auto",
        "low_latency": False,
    }


def normalize_feed_record(record: Any) -> Dict[str, Any]:
    if isinstance(record, str):
        return default_feed_profile(record)
    if isinstance(record, dict):
        url = str(record.get("url", "")).strip()
        out = default_feed_profile(url)
        out["start_muted"] = bool(record.get("start_muted", out["start_muted"]))
        out["preferred_volume"] = max(0, min(100, int(record.get("preferred_volume", out["preferred_volume"]))))
        backend = str(record.get("preferred_backend", out["preferred_backend"])).strip().lower()
        out["preferred_backend"] = backend if backend in ("auto", "qt", "embed") else "auto"
        out["low_latency"] = bool(record.get("low_latency", out["low_latency"]))
        return out
    return default_feed_profile("")


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
    show_list_manager: bool = True
    compact_ui: bool = False
    ui_scale_percent: int = 100
    max_auto_retries: int = 6
    auto_retry_backoff_ms: int = 3000
    audio_follow_selection: bool = True
    audio_fade_ms: int = 180
    show_operational_overlays: bool = True
    watchdog_enabled: bool = True
    watchdog_down_seconds: int = 20
    verbose_logging: bool = False

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
        s.show_list_manager = bool(d.get("show_list_manager", s.show_list_manager))
        s.compact_ui = bool(d.get("compact_ui", s.compact_ui))
        s.ui_scale_percent = max(80, min(160, int(d.get("ui_scale_percent", s.ui_scale_percent))))
        s.max_auto_retries = max(1, min(25, int(d.get("max_auto_retries", s.max_auto_retries))))
        s.auto_retry_backoff_ms = max(250, min(30000, int(d.get("auto_retry_backoff_ms", s.auto_retry_backoff_ms))))
        s.audio_follow_selection = bool(d.get("audio_follow_selection", s.audio_follow_selection))
        s.audio_fade_ms = max(0, min(2000, int(d.get("audio_fade_ms", s.audio_fade_ms))))
        s.show_operational_overlays = bool(d.get("show_operational_overlays", s.show_operational_overlays))
        s.watchdog_enabled = bool(d.get("watchdog_enabled", s.watchdog_enabled))
        s.watchdog_down_seconds = max(5, min(300, int(d.get("watchdog_down_seconds", s.watchdog_down_seconds))))
        s.verbose_logging = bool(d.get("verbose_logging", s.verbose_logging))
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


class QtTile(QGraphicsView):
    requestToggle = pyqtSignal(object)
    started = pyqtSignal(object)
    requestFullscreen = pyqtSignal(object)
    requestRemove = pyqtSignal(object)
    requestPip = pyqtSignal(object)
    requestActive = pyqtSignal(object)
    requestReorder = pyqtSignal(object, object)
    titleChanged = pyqtSignal(str)
    healthChanged = pyqtSignal(object)

    def __init__(
            self,
            url: str,
            title: str,
            settings: AppSettings,
            parent=None,
            muted: bool = True,
            feed_profile: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(parent)
        self.setObjectName("tile")
        self.url = url
        self.title = title or tr("Tile", "Video")
        self.is_muted = muted
        self.is_fullscreen_tile = False
        self.is_pip_tile = False
        self._settings = settings
        self.setAcceptDrops(True)
        self.feed_profile = feed_profile or {}
        self.force_backend = str(self.feed_profile.get("preferred_backend", "auto")).lower()
        if self.force_backend not in ("auto", "qt", "embed"):
            self.force_backend = "auto"
        self.low_latency = bool(self.feed_profile.get("low_latency", False))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.is_playing = False
        self._stall_retries = 0
        self.last_error = ""
        self.reconnect_count = 0
        self.health_state = "idle"
        self.last_health_change = datetime.now()
        self.last_playing_at: Optional[datetime] = None
        self.last_load_start: Optional[datetime] = None
        self.buffering_seconds = 0.0
        self.estimated_latency_ms = 0
        self.down_since: Optional[datetime] = None
        self._highlight_state: Tuple[bool, bool] = (False, False)
        self._drag_start_pos = None
        self._drop_hover = False
        self._latency_timer = QTimer(self)
        self._latency_timer.setInterval(1000)
        self._latency_timer.timeout.connect(self._tick_overlay_metrics)

        # Setup Graphics View
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QColor(0, 0, 0))

        # Video Item
        self.video_item = QGraphicsVideoItem()
        self.scene.addItem(self.video_item)

        # Overlay Widget
        self.overlay_widget = QWidget()
        self.overlay_widget.setObjectName("tileOverlay")
        self.overlay_widget.setStyleSheet("background: transparent;")

        # Status Overlay
        self.status_overlay = QLabel(self.overlay_widget)
        self.status_overlay.setObjectName("tileStatusOverlay")
        self.status_overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 160); color: white; padding: 4px; font-weight: bold;"
        )
        self.status_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_overlay.hide()

        self.health_badge = QLabel(self.overlay_widget)
        self.health_badge.setObjectName("tileHealthBadge")
        self.health_badge.setStyleSheet(
            "background-color: rgba(0, 0, 0, 160); color: white; padding: 3px 6px; border-radius: 4px; font-weight: bold;"
        )
        self.health_badge.show()

        self.ops_overlay = QLabel(self.overlay_widget)
        self.ops_overlay.setObjectName("tileOpsOverlay")
        self.ops_overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 140); color: #e8edf2; padding: 3px 6px; border-radius: 4px;"
        )
        self.ops_overlay.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.ops_overlay.setWordWrap(True)
        self.ops_overlay.show()

        # Mute Button (Top Right)
        self.overlay_mute_button = QPushButton(self.overlay_widget)
        self.overlay_mute_button.setObjectName("tileOverlayButton")
        self.overlay_mute_button.setFixedSize(28, 28)
        self.overlay_mute_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overlay_mute_button.setStyleSheet(
            "background-color: rgba(0, 0, 0, 120); border-radius: 4px; border: 1px solid rgba(255, 255, 255, 50);"
        )
        self.overlay_mute_button.clicked.connect(lambda: self.requestToggle.emit(self))
        self.overlay_mute_button.show()

        # Controls (Bottom)
        self.controls = QWidget(self.overlay_widget)
        self.controls.setObjectName("controls")
        self.controls.setAccessibleName(tr("Tile", "Controls"))
        row = QHBoxLayout(self.controls)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(6)

        self.label = QLabel(self.title, self.controls)
        self.label.setAccessibleName(tr("Tile", "Title"))
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.play_button = QPushButton("", self.controls)
        self.play_button.setObjectName("tileIconButton")
        self.play_button.setAccessibleName(tr("Tile", "Play or pause"))
        self.play_button.setFixedSize(24, 24)
        self.play_button.setIconSize(QSize(16, 16))

        self.mute_button = QPushButton("", self.controls)
        self.mute_button.setObjectName("tileIconButton")
        self.mute_button.setAccessibleName(tr("Tile", "Mute"))
        self.mute_button.setFixedSize(24, 24)
        self.mute_button.setIconSize(QSize(16, 16))

        self.reload_button = QPushButton("", self.controls)
        self.reload_button.setObjectName("tileIconButton")
        self.reload_button.setAccessibleName(tr("Tile", "Reload"))
        self.reload_button.setFixedSize(24, 24)
        self.reload_button.setIconSize(QSize(16, 16))

        self.pip_button = QPushButton("", self.controls)
        self.pip_button.setObjectName("tileIconButton")
        self.pip_button.setAccessibleName(tr("Tile", "Picture-in-Picture"))
        self.pip_button.setFixedSize(24, 24)
        self.pip_button.setIconSize(QSize(16, 16))

        self.fullscreen_button = QPushButton("", self.controls)
        self.fullscreen_button.setObjectName("tileIconButton")
        self.fullscreen_button.setAccessibleName(tr("Tile", "Fullscreen"))
        self.fullscreen_button.setFixedSize(24, 24)
        self.fullscreen_button.setIconSize(QSize(16, 16))

        self.remove_button = QPushButton("", self.controls)
        self.remove_button.setObjectName("tileIconButton")
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

        # Add Overlay to Scene via Proxy
        self.proxy = self.scene.addWidget(self.overlay_widget)
        self.proxy.setZValue(10)  # Ensure it's on top

        # Media Player
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_item)

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
        self._set_health("idle")
        self._sync_overlay_timer()
        self.controls.show()

    def _apply_tile_styles(self):
        active_audio, focused = self._highlight_state
        border = "border: 1px solid rgba(255, 255, 255, 0.08);"
        shadow = "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(28, 31, 37, 255), stop:1 rgba(16, 18, 22, 255));"
        if active_audio:
            border = "border: 3px solid #2f8cff;"
        elif focused:
            border = "border: 2px solid rgba(158, 198, 255, 0.95);"
        elif self._drop_hover:
            border = "border: 2px dashed #f0c75e;"
        self.setStyleSheet(
            f"""
            QGraphicsView#tile {{ border-radius: 14px; {shadow} {border} }}
            QWidget#controls {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(10, 12, 16, 120),
                    stop:1 rgba(10, 12, 16, 210));
                border-top: 1px solid rgba(255, 255, 255, 0.08);
            }}
            QLabel {{ padding-left: 8px; color: white; }}
            QWidget#controls QPushButton#tileIconButton {{
                border: none;
                padding: 0px;
                margin: 0px;
                background: transparent;
                border-radius: 8px;
            }}
            QWidget#controls QPushButton#tileIconButton:hover {{ background: rgba(255, 255, 255, 0.10); }}
            QPushButton#tileOverlayButton:hover {{ background-color: rgba(255, 255, 255, 0.16); }}
            """
        )

    def set_highlight_state(self, active_audio: bool, focused: bool) -> None:
        state = (active_audio, focused)
        if state == self._highlight_state:
            return
        self._highlight_state = state
        self._apply_tile_styles()

    def _sync_overlay_timer(self) -> None:
        should_run = self._settings.show_operational_overlays and self.isVisible()
        if should_run:
            if not self._latency_timer.isActive():
                self._latency_timer.start()
            return
        self._latency_timer.stop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        rect = QRectF(self.rect())
        self.scene.setSceneRect(rect)
        self.video_item.setSize(rect.size())
        self.proxy.setGeometry(rect)

        # Reposition internal overlay elements
        w = self.width()
        h = self.height()
        
        if self.status_overlay:
            self.status_overlay.setGeometry(0, 0, w, h)

        if hasattr(self, "overlay_mute_button"):
            self.overlay_mute_button.move(w - self.overlay_mute_button.width() - 8, 8)

        if hasattr(self, "health_badge"):
            self.health_badge.move(8, 8)

        if hasattr(self, "ops_overlay"):
            self.ops_overlay.setGeometry(8, 38, min(320, max(120, w - 16)), 52)

        if hasattr(self, "controls"):
            ch = self.controls.sizeHint().height()
            self.controls.setGeometry(0, h - ch, w, ch)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
            self.requestActive.emit(self)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        if self._drag_start_pos is None:
            super().mouseMoveEvent(event)
            return
        if (event.pos() - self._drag_start_pos).manhattanLength() < QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return
        self._start_drag()
        self._drag_start_pos = None
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_start_pos = None
        super().mouseReleaseEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_overlay_timer()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sync_overlay_timer()

    def _start_drag(self) -> None:
        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(self.title)
        drag.setMimeData(mime)
        try:
            drag.setPixmap(self.grab())
        except Exception:
            pass
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event):
        source = event.source()
        if isinstance(source, QtTile) and source is not self:
            self._drop_hover = True
            self._apply_tile_styles()
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        source = event.source()
        if isinstance(source, QtTile) and source is not self:
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._drop_hover = False
        self._apply_tile_styles()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._drop_hover = False
        self._apply_tile_styles()
        source = event.source()
        if isinstance(source, QtTile) and source is not self:
            self.requestReorder.emit(source, self)
            event.acceptProposedAction()
            return
        event.ignore()

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

    def _set_health(self, state: str, error: str = ""):
        self.health_state = state
        self.last_health_change = datetime.now()
        if error:
            self.last_error = error
        if state in ("error", "reconnecting", "stalled", "loading", "buffering"):
            if self.down_since is None:
                self.down_since = datetime.now()
        elif state == "live":
            self.down_since = None

        colors = {
            "live": "#2e7d32",
            "loading": "#1565c0",
            "buffering": "#ef6c00",
            "stalled": "#ef6c00",
            "reconnecting": "#6a1b9a",
            "error": "#b71c1c",
            "idle": "#37474f",
        }
        text_map = {
            "live": tr("Tile", "LIVE"),
            "loading": tr("Tile", "LOADING"),
            "buffering": tr("Tile", "BUFFERING"),
            "stalled": tr("Tile", "STALLED"),
            "reconnecting": tr("Tile", "RECONNECTING"),
            "error": tr("Tile", "ERROR"),
            "idle": tr("Tile", "IDLE"),
        }
        color = colors.get(state, "#37474f")
        self.health_badge.setText(text_map.get(state, state.upper()))
        self.health_badge.setStyleSheet(
            f"background-color: {color}; color: white; padding: 3px 6px; border-radius: 4px; font-weight: bold;"
        )
        self._sync_overlay_timer()
        self._refresh_ops_overlay()
        self.healthChanged.emit(self)

    def _tick_overlay_metrics(self):
        if not self._settings.show_operational_overlays:
            return
        if self.health_state in ("buffering", "loading", "stalled", "reconnecting"):
            self.buffering_seconds += 1.0
        self._refresh_ops_overlay()

    def _refresh_ops_overlay(self):
        if not self._settings.show_operational_overlays:
            self.health_badge.hide()
            self.ops_overlay.hide()
            return
        self.health_badge.show()
        self.ops_overlay.show()
        now_txt = datetime.now().strftime("%H:%M:%S")
        down_txt = "-"
        if self.down_since is not None:
            down_txt = f"{int((datetime.now() - self.down_since).total_seconds())}s"
        err_txt = self.last_error if self.last_error else "-"
        ops = (
            f"{tr('Tile', 'Clock')}: {now_txt}    "
            f"{tr('Tile', 'Latency')}: {self.estimated_latency_ms}ms\n"
            f"{tr('Tile', 'Reconnects')}: {self.reconnect_count}    "
            f"{tr('Tile', 'Down')}: {down_txt}\n"
            f"{tr('Tile', 'Error')}: {err_txt}"
        )
        self.ops_overlay.setText(ops)

    def on_media_status_changed(self, status: QMediaPlayer.MediaStatus):
        if status == QMediaPlayer.MediaStatus.LoadingMedia:
            self.last_load_start = datetime.now()
            self.label.setText(f"{self.title}  {tr('Tile', 'Loading')}")
            self._set_health("loading")
            self._set_status_overlay(tr("Tile", "Loading"))
        elif status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.label.setText(self.title)
            self._stall_retries = 0
            self.last_playing_at = datetime.now()
            if self.last_load_start is not None:
                self.estimated_latency_ms = int(
                    max(0.0, (datetime.now() - self.last_load_start).total_seconds()) * 1000)
            self._set_health("live")
            self._set_status_overlay("")
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.label.setText(f"{self.title}  {tr('Tile', 'Invalid media')}")
            self._stall_retries += 1
            self._set_health("error", tr("Tile", "Invalid media"))
            self._set_status_overlay(tr("Tile", "Invalid media"))
            if self._stall_retries <= self._settings.max_auto_retries:
                self.reconnect_count += 1
                QTimer.singleShot(self._settings.auto_retry_backoff_ms, self.safe_reload)
        elif status == QMediaPlayer.MediaStatus.StalledMedia:
            self.label.setText(f"{self.title}  {tr('Tile', 'Stalled')}")
            self._stall_retries += 1
            self._set_health("stalled", tr("Tile", "Stalled"))
            self._set_status_overlay(tr("Tile", "Stalled, retrying"))
            if self._stall_retries <= self._settings.max_auto_retries:
                self.reconnect_count += 1
                self._set_health("reconnecting")
                QTimer.singleShot(self._settings.auto_retry_backoff_ms, self.safe_reload)
        elif status == QMediaPlayer.MediaStatus.BufferingMedia:
            self.label.setText(f"{self.title}  {tr('Tile', 'Buffering')}")
            self._set_health("buffering")
            self._set_status_overlay(tr("Tile", "Buffering"))

    def on_playback_state_changed(self, state: QMediaPlayer.PlaybackState):
        self.is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        if self.is_playing:
            self.last_playing_at = datetime.now()
            self._set_health("live")
        self._refresh_icons()

    def toggle_play(self):
        state = self.player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def play_url(self, url: str):
        chosen = self.force_backend if self.force_backend != "auto" else choose_backend(url, self._settings)
        src = build_embed_or_watch(url)
        self.last_load_start = datetime.now()
        self._set_health("loading")

        if chosen in ("qt", "embed") and is_youtube_url(src):
            if YT_AVAILABLE:
                self.label.setText(f"{self.title}  {tr('Tile', 'Resolving')}")
                self._set_status_overlay(tr("Tile", "Resolving"))
                self._yt_worker = YtResolveWorker(src, self)
                self._yt_worker.resolved.connect(lambda direct: self._apply_source(QUrl(direct)))
                self._yt_worker.failed.connect(
                    lambda _msg: self.label.setText(f"{self.title}  {tr('Tile', 'Resolve failed')}")
                )
                self._yt_worker.failed.connect(lambda _msg: self._set_health("error", tr("Tile", "Resolve failed")))
                self._yt_worker.failed.connect(lambda _msg: self._set_status_overlay(tr("Tile", "Resolve failed")))
                self._yt_worker.start()
                return
            else:
                self.label.setText(f"{self.title}  {tr('Tile', 'yt dlp not installed')}")
                self._set_health("error", tr("Tile", "yt dlp not installed"))
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

    def set_overlay_visibility(self, show_ops: bool):
        if self._settings.show_operational_overlays == show_ops:
            self._sync_overlay_timer()
            return
        self._settings.show_operational_overlays = show_ops
        self._sync_overlay_timer()
        self._refresh_ops_overlay()

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
        self._set_health("reconnecting")
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
        self.player.setVideoOutput(self.video_item)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.player.playbackStateChanged.connect(self.on_playback_state_changed)

        if self.is_muted:
            self.audio.setMuted(True)
            self.audio.setVolume(0.0)

        self.play_url(self.url)

    def stop(self):
        try:
            self._latency_timer.stop()
        except Exception:
            pass
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
    def __init__(self, parent=None, name="", url="", profile: Optional[Dict[str, Any]] = None,
                 show_profile: bool = True):
        super().__init__(parent)
        self.setWindowTitle(tr("Feed", "Add or Edit Feed"))
        form = QFormLayout(self)
        p = normalize_feed_record(profile or {"url": url})
        self.name_input = QLineEdit(name)
        self.url_input = QLineEdit(p["url"] if p["url"] else url)
        self.name_label = QLabel(tr("Feed", "Name"))
        form.addRow(self.name_label, self.name_input)
        form.addRow(tr("Feed", "URL"), self.url_input)
        self.start_muted = QCheckBox(tr("Feed", "Start muted"))
        self.start_muted.setChecked(bool(p["start_muted"]))
        self.preferred_volume = QSlider(Qt.Orientation.Horizontal)
        self.preferred_volume.setRange(0, 100)
        self.preferred_volume.setValue(int(p["preferred_volume"]))
        self.preferred_backend = QComboBox()
        self.preferred_backend.addItems(["auto", "qt", "embed"])
        self.preferred_backend.setCurrentText(str(p["preferred_backend"]))
        self.low_latency = QCheckBox(tr("Feed", "Low latency mode"))
        self.low_latency.setChecked(bool(p["low_latency"]))
        if show_profile:
            form.addRow("", self.start_muted)
            form.addRow(tr("Feed", "Preferred volume"), self.preferred_volume)
            form.addRow(tr("Feed", "Preferred backend"), self.preferred_backend)
            form.addRow("", self.low_latency)
        box = QDialogButtonBox()
        box.setStandardButtons(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addWidget(box)

    def set_name_label(self, text: str):
        self.name_label.setText(text)

    def get_feed_data(self):
        return self.name_input.text(), self.url_input.text()

    def get_profile_data(self) -> Dict[str, Any]:
        return {
            "url": self.url_input.text().strip(),
            "start_muted": bool(self.start_muted.isChecked()),
            "preferred_volume": int(self.preferred_volume.value()),
            "preferred_backend": self.preferred_backend.currentText(),
            "low_latency": bool(self.low_latency.isChecked()),
        }


class PlaylistViewerDialog(QDialog):
    def __init__(self, parent, channels):
        super().__init__(parent)
        self.setWindowTitle(tr("Playlist", "Select Channels"))
        self.setMinimumSize(400, 500)
        layout = QVBoxLayout(self)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(tr("Playlist", "Search channels..."))
        self.search_input.textChanged.connect(self.filter_channels)
        layout.addWidget(self.search_input)

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

    def filter_channels(self, text: str):
        text = text.lower().strip()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            name = item.text().lower()
            item.setHidden(bool(text and text not in name))

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
        self.tabs.setObjectName("listTabs")
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
        self.toolbar.setObjectName("listToolbar")
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
        self.news_feed_list_widget.setObjectName("feedList")
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
        self.playlists_toolbar.setObjectName("listToolbar")
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
        self.playlists_list_widget.setObjectName("playlistList")
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
        self.grid_toolbar.setObjectName("listToolbar")
        self.grid_toolbar_layout = QHBoxLayout(self.grid_toolbar)
        self.grid_toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_toolbar_layout.setSpacing(6)

        self.remove_from_grid_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), tr("List", "Remove"))
        self.remove_from_grid_button.setAccessibleName(tr("List", "Remove from grid"))
        self.grid_toolbar_layout.addWidget(self.remove_from_grid_button)
        self.grid_toolbar_layout.addStretch()

        self.grid_list_widget = QListWidget()
        self.grid_list_widget.setObjectName("gridList")
        self.grid_list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid_list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.grid_list_widget.setDragEnabled(True)
        self.grid_list_widget.setAcceptDrops(True)
        self.grid_list_widget.setDropIndicatorShown(True)
        self.grid_list_widget.setAccessibleName(tr("List", "Grid Items"))

        self.grid_layout.addWidget(self.grid_toolbar, 0)
        self.grid_layout.addWidget(self.grid_list_widget, 1)
        self.tabs.addTab(self.grid_tab, tr("List", "Grid"))

        self.scenes_tab = QWidget()
        self.scenes_layout = QVBoxLayout(self.scenes_tab)
        self.scenes_layout.setContentsMargins(6, 6, 6, 6)
        self.scenes_layout.setSpacing(6)

        self.scenes_toolbar = QWidget()
        self.scenes_toolbar.setObjectName("listToolbar")
        self.scenes_toolbar_layout = QHBoxLayout(self.scenes_toolbar)
        self.scenes_toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.scenes_toolbar_layout.setSpacing(6)

        self.play_scene_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay), tr("List", "Play"))
        self.delete_scene_button = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon),
                                               tr("List", "Delete"))
        self.play_scene_button.setAccessibleName(tr("List", "Play scene"))
        self.delete_scene_button.setAccessibleName(tr("List", "Delete scene"))

        self.scenes_toolbar_layout.addWidget(self.play_scene_button)
        self.scenes_toolbar_layout.addWidget(self.delete_scene_button)
        self.scenes_toolbar_layout.addStretch()

        self.scenes_list_widget = QListWidget()
        self.scenes_list_widget.setObjectName("sceneList")
        self.scenes_list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.scenes_list_widget.setAccessibleName(tr("List", "Scenes"))

        self.scenes_layout.addWidget(self.scenes_toolbar, 0)
        self.scenes_layout.addWidget(self.scenes_list_widget, 1)
        self.tabs.addTab(self.scenes_tab, tr("List", "Scenes"))

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
        self.setMinimumSize(760, 520)
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
        self.setMinimumWidth(540)
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
        self.layout_mode.addItems(LAYOUT_MODES)

        self.show_list_manager = QCheckBox(tr("Settings", "Show Manage Lists"))
        self.compact_ui = QCheckBox(tr("Settings", "Compact UI"))
        self.audio_follow_selection = QCheckBox(tr("Settings", "Audio follows selected tile"))
        self.show_operational_overlays = QCheckBox(tr("Settings", "Show tile operational overlays"))
        self.watchdog_enabled = QCheckBox(tr("Settings", "Enable stream watchdog"))
        self.verbose_logging = QCheckBox(tr("Settings", "Enable verbose background logging"))

        self.ui_scale_percent = QSlider(Qt.Orientation.Horizontal)
        self.ui_scale_percent.setRange(80, 160)
        self.audio_fade_ms = QSlider(Qt.Orientation.Horizontal)
        self.audio_fade_ms.setRange(0, 2000)
        self.max_auto_retries = QSlider(Qt.Orientation.Horizontal)
        self.max_auto_retries.setRange(1, 25)
        self.auto_retry_backoff_ms = QSlider(Qt.Orientation.Horizontal)
        self.auto_retry_backoff_ms.setRange(250, 30000)
        self.auto_retry_backoff_ms.setSingleStep(250)
        self.watchdog_down_seconds = QSlider(Qt.Orientation.Horizontal)
        self.watchdog_down_seconds.setRange(5, 300)

        self.populate_fields()

        form.addRow(tr("Settings", "Audio policy"), self.audio_policy)
        form.addRow(tr("Settings", "Default volume"), self.volume_default)
        form.addRow(tr("Settings", "YouTube mode"), self.yt_mode)
        form.addRow(tr("Settings", "Theme"), self.theme)
        form.addRow(tr("Settings", "Layout"), self.layout_mode)
        form.addRow(tr("Settings", "UI scale %"), self.ui_scale_percent)
        form.addRow(tr("Settings", "Audio fade ms"), self.audio_fade_ms)
        form.addRow(tr("Settings", "Auto retries"), self.max_auto_retries)
        form.addRow(tr("Settings", "Retry backoff ms"), self.auto_retry_backoff_ms)
        form.addRow(tr("Settings", "Watchdog down sec"), self.watchdog_down_seconds)
        form.addRow("", self.privacy_embed_only_youtube)
        form.addRow("", self.pause_others_in_fullscreen)
        form.addRow("", self.show_list_manager)
        form.addRow("", self.compact_ui)
        form.addRow("", self.audio_follow_selection)
        form.addRow("", self.show_operational_overlays)
        form.addRow("", self.watchdog_enabled)
        form.addRow("", self.verbose_logging)

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
        if s.layout_mode in LAYOUT_MODES:
            self.layout_mode.setCurrentText(s.layout_mode)
        else:
            self.layout_mode.setCurrentText("auto")
        self.show_list_manager.setChecked(s.show_list_manager)
        self.compact_ui.setChecked(s.compact_ui)
        self.ui_scale_percent.setValue(s.ui_scale_percent)
        self.max_auto_retries.setValue(s.max_auto_retries)
        self.auto_retry_backoff_ms.setValue(s.auto_retry_backoff_ms)
        self.audio_follow_selection.setChecked(s.audio_follow_selection)
        self.audio_fade_ms.setValue(s.audio_fade_ms)
        self.show_operational_overlays.setChecked(s.show_operational_overlays)
        self.watchdog_enabled.setChecked(s.watchdog_enabled)
        self.watchdog_down_seconds.setValue(s.watchdog_down_seconds)
        self.verbose_logging.setChecked(s.verbose_logging)

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
        s.show_list_manager = self.show_list_manager.isChecked()
        s.compact_ui = bool(self.compact_ui.isChecked())
        s.ui_scale_percent = int(self.ui_scale_percent.value())
        s.max_auto_retries = int(self.max_auto_retries.value())
        s.auto_retry_backoff_ms = int(self.auto_retry_backoff_ms.value())
        s.audio_follow_selection = bool(self.audio_follow_selection.isChecked())
        s.audio_fade_ms = int(self.audio_fade_ms.value())
        s.show_operational_overlays = bool(self.show_operational_overlays.isChecked())
        s.watchdog_enabled = bool(self.watchdog_enabled.isChecked())
        s.watchdog_down_seconds = int(self.watchdog_down_seconds.value())
        s.verbose_logging = bool(self.verbose_logging.isChecked())
        configure_logging(self._sm.logger, s.verbose_logging)
        self._sm.save()


class PipWindow(QWidget):
    def __init__(self, tile: QtTile, main_window: QMainWindow):
        super().__init__()
        self.tile = tile
        self.main_window = main_window
        self.should_reattach = True
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setWindowTitle(f"PiP  {tile.title}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tile)

        self.tile.set_is_pip(True)
        self.show()

    def closeEvent(self, event):
        if self.tile is not None:
            self.tile.set_is_pip(False)
            self.layout().removeWidget(self.tile)
            self.tile.setParent(None)
            if self.should_reattach and self.tile in self.main_window.video_widgets:
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
        self.central_widget_container.setObjectName("gridHost")
        self.central_widget_container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        the_layout = QGridLayout(self.central_widget_container)
        the_layout.setContentsMargins(0, 0, 0, 0)
        the_layout.setSpacing(4)
        self.grid_layout = the_layout
        self.setCentralWidget(self.central_widget_container)

        self.grid_empty_state = QLabel(
            tr("UI",
               "No streams in the board yet.\nOpen Manage Lists or paste a stream URL above to start building a scene."),
            self.central_widget_container,
        )
        self.grid_empty_state.setObjectName("gridEmptyState")
        self.grid_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.grid_empty_state.setWordWrap(True)

        self.status_bar = self.statusBar()
        self.status_label = QLabel()
        self.status_label.setObjectName("statusLabel")
        self.status_bar.addPermanentWidget(self.status_label)
        self.status_label.setText(tr("UI", "Ready"))

        self.video_widgets: list[QtTile] = []
        self.currently_unmuted: Optional[QtTile] = None
        self.focused_tile: Optional[QtTile] = None
        self.active_volume: int = int(self.settings.volume_default)
        self.allow_auto_select: bool = True
        self._audio_enforce_generation = 0
        self.fullscreen_tile: Optional[QtTile] = None
        self.main_toolbar: Optional[QToolBar] = None
        self._is_clearing = False
        self.url_action: Optional[QWidgetAction] = None
        self.scenes: Dict[str, Dict[str, Any]] = {}
        self.last_watchdog_alert: Dict[str, datetime] = {}
        self.scene_combo: Optional[QComboBox] = None
        self._last_grid_signature: Optional[Tuple[Any, ...]] = None
        self._grid_list_signature: Optional[Tuple[Any, ...]] = None

        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.setInterval(2000)
        self.watchdog_timer.timeout.connect(self._run_watchdog)
        self.watchdog_timer.start()

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
        self.list_manager.play_scene_button.clicked.connect(self.play_selected_scene_from_list)
        self.list_manager.delete_scene_button.clicked.connect(self.delete_selected_scene_from_list)
        self.list_manager.scenes_list_widget.itemDoubleClicked.connect(
            lambda _item: self.play_selected_scene_from_list())
        self.list_manager.visibilityChanged.connect(self.on_list_manager_visibility_changed)

        self._build_top_toolbar()
        self._build_menus()

        self._queue = deque()
        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(40)
        self._queue_timer.timeout.connect(self._process_queue)
        self._audio_retry_timer = QTimer(self)
        self._audio_retry_timer.setInterval(self.AUDIO_RETRY_DELAY_MS)
        self._audio_retry_timer.timeout.connect(self._run_audio_retry)
        self._audio_retry_generation = 0
        self._audio_retry_remaining = 0

        self.news_feeds: Dict[str, Dict[str, Any]] = {}
        self.playlists: Dict[str, str] = {}
        self.load_news_feeds()
        self.load_playlists()
        self.list_manager.tabs.setCurrentIndex(0)
        self.list_manager.setVisible(self.settings.show_list_manager)

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
        self._apply_ui_density()

    def refresh_ui(self):
        self.sm.load()
        self.settings = self.sm.settings
        self.apply_theme()
        self._apply_ui_density()
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
            palette_name = "dark"
        else:
            app.setPalette(QApplication.style().standardPalette())
            palette_name = "light"
        app.setStyleSheet(self._build_app_stylesheet(palette_name))

    def _build_app_stylesheet(self, palette_name: str) -> str:
        if palette_name == "dark":
            colors = {
                "window": "#111419",
                "panel": "#171c23",
                "panel_alt": "#1d2430",
                "panel_soft": "#202836",
                "input": "#111721",
                "border": "#2a3342",
                "border_strong": "#3a4659",
                "text": "#f2f5f8",
                "text_muted": "#9aa7b8",
                "accent": "#2f8cff",
                "accent_soft": "#173861",
                "danger": "#a94747",
                "hover": "rgba(255, 255, 255, 0.06)",
            }
        else:
            colors = {
                "window": "#eef3f8",
                "panel": "#f7f9fc",
                "panel_alt": "#ffffff",
                "panel_soft": "#e8eef6",
                "input": "#ffffff",
                "border": "#ced8e4",
                "border_strong": "#b5c3d3",
                "text": "#14202b",
                "text_muted": "#5e6f82",
                "accent": "#0d6efd",
                "accent_soft": "#d9e8ff",
                "danger": "#c24b4b",
                "hover": "rgba(13, 110, 253, 0.08)",
            }
        return f"""
            QToolTip {{
                color: {colors["text"]};
                background-color: {colors["panel_alt"]};
                border: 1px solid {colors["border_strong"]};
                padding: 6px 8px;
            }}
            QMainWindow, QWidget {{
                color: {colors["text"]};
            }}
            QMainWindow {{
                background: {colors["window"]};
            }}
            QWidget#gridHost {{
                background: {colors["window"]};
            }}
            QLabel#gridEmptyState {{
                color: {colors["text_muted"]};
                font-size: 18px;
                font-weight: 600;
                padding: 28px;
                border: 1px dashed {colors["border_strong"]};
                border-radius: 18px;
                background: {colors["panel"]};
            }}
            QStatusBar {{
                background: {colors["panel"]};
                border-top: 1px solid {colors["border"]};
            }}
            QLabel#statusLabel {{
                color: {colors["text_muted"]};
                padding: 2px 6px;
            }}
            QMenu {{
                background-color: {colors["panel_alt"]};
                color: {colors["text"]};
                border: 1px solid {colors["border"]};
                padding: 6px;
            }}
            QMenu::item {{
                background-color: transparent;
                padding: 7px 16px;
                border-radius: 8px;
            }}
            QMenu::item:selected {{
                background-color: {colors["hover"]};
                color: {colors["text"]};
            }}
            QMenu::separator {{
                height: 1px;
                background: {colors["border"]};
                margin: 6px 8px;
            }}
            QMenuBar {{
                background-color: {colors["panel"]};
                color: {colors["text"]};
                border-bottom: 1px solid {colors["border"]};
            }}
            QMenuBar::item {{
                background-color: transparent;
                padding: 6px 12px;
                margin: 2px 2px;
                border-radius: 8px;
            }}
            QMenuBar::item:selected {{
                background-color: {colors["hover"]};
            }}
            QToolBar {{
                background: {colors["panel"]};
                border: none;
                border-bottom: 1px solid {colors["border"]};
                padding: 10px 12px;
                spacing: 8px;
            }}
            QWidget#toolbarGroup {{
                background: {colors["panel_alt"]};
                border: 1px solid {colors["border"]};
                border-radius: 14px;
            }}
            QWidget#toolbarGroup QLabel#toolbarSectionLabel {{
                color: {colors["text_muted"]};
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.08em;
                text-transform: uppercase;
                padding-right: 6px;
            }}
            QLineEdit, QComboBox, QListWidget, QTextEdit {{
                background: {colors["input"]};
                color: {colors["text"]};
                border: 1px solid {colors["border"]};
                border-radius: 10px;
                padding: 7px 10px;
                selection-background-color: {colors["accent"]};
                selection-color: #ffffff;
            }}
            QLineEdit:focus, QComboBox:focus, QListWidget:focus, QTextEdit:focus {{
                border: 1px solid {colors["accent"]};
            }}
            QLineEdit#urlInput {{
                min-width: 280px;
                padding: 9px 12px;
            }}
            QComboBox::drop-down {{
                border: none;
                width: 24px;
            }}
            QComboBox QAbstractItemView {{
                background: {colors["panel_alt"]};
                color: {colors["text"]};
                border: 1px solid {colors["border"]};
                selection-background-color: {colors["accent"]};
                selection-color: #ffffff;
            }}
            QListWidget {{
                outline: 0;
                padding: 8px;
            }}
            QListWidget::item {{
                padding: 7px 8px;
                border-radius: 8px;
                margin: 1px 0px;
            }}
            QListWidget::item:selected {{
                background: {colors["accent_soft"]};
                color: {colors["text"]};
            }}
            QListWidget::item:hover {{
                background: {colors["hover"]};
            }}
            QPushButton {{
                background: {colors["panel_soft"]};
                color: {colors["text"]};
                border: 1px solid {colors["border"]};
                border-radius: 10px;
                padding: 8px 12px;
                min-height: 18px;
            }}
            QPushButton:hover {{
                background: {colors["hover"]};
                border-color: {colors["border_strong"]};
            }}
            QPushButton:pressed {{
                background: {colors["accent_soft"]};
            }}
            QPushButton#primaryButton {{
                background: {colors["accent"]};
                color: #ffffff;
                border-color: {colors["accent"]};
                font-weight: 700;
            }}
            QPushButton#dangerButton {{
                color: #ffffff;
                background: {colors["danger"]};
                border-color: {colors["danger"]};
            }}
            QSlider::groove:horizontal {{
                border: none;
                height: 6px;
                background: {colors["border"]};
                border-radius: 3px;
            }}
            QSlider::sub-page:horizontal {{
                background: {colors["accent"]};
                border-radius: 3px;
            }}
            QSlider::handle:horizontal {{
                background: {colors["panel_alt"]};
                border: 2px solid {colors["accent"]};
                width: 14px;
                margin: -5px 0;
                border-radius: 9px;
            }}
            QDockWidget#listManager {{
                color: {colors["text"]};
            }}
            QDockWidget#listManager::title {{
                background: {colors["panel"]};
                text-align: left;
                padding: 10px 12px;
                border-bottom: 1px solid {colors["border"]};
                font-weight: 700;
            }}
            QTabWidget#listTabs::pane {{
                border: none;
                background: {colors["panel"]};
            }}
            QTabBar::tab {{
                background: transparent;
                color: {colors["text_muted"]};
                padding: 8px 12px;
                margin-right: 4px;
                border-bottom: 2px solid transparent;
            }}
            QTabBar::tab:selected {{
                color: {colors["text"]};
                border-bottom-color: {colors["accent"]};
                font-weight: 700;
            }}
            QWidget#listToolbar {{
                background: {colors["panel_alt"]};
                border: 1px solid {colors["border"]};
                border-radius: 12px;
                padding: 4px;
            }}
            QDialog {{
                background: {colors["window"]};
            }}
            QDialogButtonBox {{
                border-top: 1px solid {colors["border"]};
                padding-top: 12px;
            }}
        """

    @staticmethod
    def _toolbar_group(title: str) -> tuple[QWidget, QHBoxLayout]:
        group = QWidget()
        group.setObjectName("toolbarGroup")
        layout = QHBoxLayout(group)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("toolbarSectionLabel")
        layout.addWidget(label)
        return group, layout

    def _apply_ui_density(self):
        scale = max(80, min(160, int(self.settings.ui_scale_percent)))
        compact = bool(self.settings.compact_ui)
        base_icon = self.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize) or 16
        icon_size = max(14, int(base_icon * (scale / 100.0)))
        spacing = 6 if compact else 10
        if self.main_toolbar:
            self.main_toolbar.setIconSize(QSize(icon_size, icon_size))
        self.grid_layout.setSpacing(spacing)

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
        act_command = QAction(tr("Menu", "Command Palette"), self)
        act_command.setShortcut(QKeySequence("Ctrl+K"))
        act_command.triggered.connect(self.open_command_palette)
        tools.addAction(act_command)

        act_save_scene = QAction(tr("Menu", "Save Scene"), self)
        act_save_scene.triggered.connect(self.save_scene_prompt)
        tools.addAction(act_save_scene)

        act_delete_scene = QAction(tr("Menu", "Delete Scene"), self)
        act_delete_scene.triggered.connect(self.delete_scene_prompt)
        tools.addAction(act_delete_scene)

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
        self.manage_lists_button.setChecked(self.settings.show_list_manager)
        self.manage_lists_button.setToolTip(tr("UI", "Show or hide list manager"))
        self.manage_lists_button.toggled.connect(self.list_manager.setVisible)
        self.list_manager.visibilityChanged.connect(self.manage_lists_button.setChecked)
        self.manage_lists_button.setObjectName("primaryButton")

        self.url_input = QLineEdit()
        self.url_input.setObjectName("urlInput")
        self.url_input.setPlaceholderText(tr("UI", "Enter URL or iframe"))
        self.url_input.setToolTip(tr("UI", "Paste a stream URL or iframe"))
        self.url_action = QWidgetAction(self)
        self.url_action.setDefaultWidget(self.url_input)

        self.add_video_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton),
            tr("UI", "Add Video"),
        )
        self.add_video_button.setObjectName("primaryButton")
        self.add_video_button.clicked.connect(self.add_video_from_input)

        self.volume_label = QLabel(tr("UI", "Volume"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setSingleStep(1)
        self.volume_slider.setPageStep(5)
        self.volume_slider.setFixedWidth(180)
        self.volume_slider.setToolTip(tr("UI", "Controls active tile volume"))
        self.volume_slider.valueChanged.connect(self.on_volume_changed)

        self.mute_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolumeMuted),
            tr("UI", "Mute All"),
        )
        self.mute_all_button.clicked.connect(self.mute_all_tiles)

        self.next_audio_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward),
            tr("UI", "Next Audio"),
        )
        self.next_audio_button.clicked.connect(self.cycle_active_audio)

        self.layout_combo = QComboBox()
        self.layout_combo.addItems(LAYOUT_MODES)
        self.layout_combo.setToolTip(tr("UI", "Grid layout"))
        self.layout_combo.setCurrentText(self.settings.layout_mode)
        self.layout_combo.currentTextChanged.connect(self.on_layout_mode_changed)

        self.scene_combo = QComboBox()
        self.scene_combo.setMinimumWidth(160)
        self.scene_combo.setToolTip(tr("UI", "Saved scenes"))
        self.scene_combo.currentTextChanged.connect(self.apply_scene_by_name)

        self.save_scene_button = QPushButton(tr("UI", "Save Scene"))
        self.save_scene_button.clicked.connect(self.save_scene_prompt)

        self.fullscreen_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton),
            tr("UI", "Fullscreen"),
        )
        self.fullscreen_button.clicked.connect(self.toggle_grid_fullscreen)

        self.reload_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
            tr("UI", "Reload All"),
        )
        self.reload_all_button.clicked.connect(self.reload_all_videos)

        self.remove_all_button = QPushButton(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon),
            tr("UI", "Remove All"),
        )
        self.remove_all_button.setObjectName("dangerButton")
        self.remove_all_button.clicked.connect(self.remove_all_videos)

        sources_group, sources_layout = self._toolbar_group(tr("UI", "Sources"))
        sources_layout.addWidget(self.manage_lists_button)
        sources_layout.addWidget(self.url_input)
        sources_layout.addWidget(self.add_video_button)
        tb.addWidget(sources_group)

        audio_group, audio_layout = self._toolbar_group(tr("UI", "Audio"))
        audio_layout.addWidget(self.volume_label)
        audio_layout.addWidget(self.volume_slider)
        audio_layout.addWidget(self.mute_all_button)
        audio_layout.addWidget(self.next_audio_button)
        tb.addWidget(audio_group)

        layout_group, layout_layout = self._toolbar_group(tr("UI", "Layout"))
        layout_layout.addWidget(self.layout_combo)
        layout_layout.addWidget(self.scene_combo)
        layout_layout.addWidget(self.save_scene_button)
        tb.addWidget(layout_group)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        session_group, session_layout = self._toolbar_group(tr("UI", "Session"))
        session_layout.addWidget(self.fullscreen_button)
        session_layout.addWidget(self.reload_all_button)
        session_layout.addWidget(self.remove_all_button)
        tb.addWidget(session_group)

        self.shortcut_add_all_feeds = QAction(self)
        self.shortcut_add_all_feeds.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.shortcut_add_all_feeds.triggered.connect(self.add_all_feeds)
        self.addAction(self.shortcut_add_all_feeds)

        self.shortcut_delete = QAction(self)
        self.shortcut_delete.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        self.shortcut_delete.triggered.connect(self.remove_video_from_grid_list)
        self.addAction(self.shortcut_delete)

        self.shortcut_fullscreen = QAction(self)
        self.shortcut_fullscreen.setShortcut(QKeySequence("F11"))
        self.shortcut_fullscreen.triggered.connect(self.toggle_grid_fullscreen)
        self.addAction(self.shortcut_fullscreen)

        self.shortcut_command_palette = QAction(self)
        self.shortcut_command_palette.setShortcut(QKeySequence("Ctrl+K"))
        self.shortcut_command_palette.triggered.connect(self.open_command_palette)
        self.addAction(self.shortcut_command_palette)

        for idx, mode in enumerate(LAYOUT_MODES, start=1):
            action = QAction(self)
            action.setShortcut(QKeySequence(f"Ctrl+{idx}"))
            action.triggered.connect(lambda _checked=False, m=mode: self.set_layout_mode(m))
            self.addAction(action)

        self.shortcut_move_left = QAction(self)
        self.shortcut_move_left.setShortcut(QKeySequence("Alt+Left"))
        self.shortcut_move_left.triggered.connect(lambda: self.move_focused_tile(-1))
        self.addAction(self.shortcut_move_left)

        self.shortcut_move_right = QAction(self)
        self.shortcut_move_right.setShortcut(QKeySequence("Alt+Right"))
        self.shortcut_move_right.triggered.connect(lambda: self.move_focused_tile(1))
        self.addAction(self.shortcut_move_right)

    def on_layout_mode_changed(self, mode: str):
        self.settings.layout_mode = mode
        self.sm.save()
        self.update_grid()

    def set_layout_mode(self, mode: str):
        if mode not in LAYOUT_MODES:
            return
        self.layout_combo.setCurrentText(mode)

    def move_focused_tile(self, delta: int):
        if self.focused_tile not in self.video_widgets:
            return
        idx = self.video_widgets.index(self.focused_tile)
        new_idx = max(0, min(len(self.video_widgets) - 1, idx + delta))
        if new_idx == idx:
            return
        self.video_widgets[idx], self.video_widgets[new_idx] = self.video_widgets[new_idx], self.video_widgets[idx]
        self.update_grid()

    def _scene_payload(self) -> Dict[str, Any]:
        return {
            "layout_mode": self.settings.layout_mode,
            "audio_policy": self.settings.audio_policy,
            "active_volume": int(self.active_volume),
            "videos": [(w.url, w.title, w.feed_profile) for w in self.video_widgets],
        }

    def _refresh_scene_combo(self):
        if self.scene_combo:
            current = self.scene_combo.currentText()
            self.scene_combo.blockSignals(True)
            self.scene_combo.clear()
            self.scene_combo.addItem("")
            for name in sorted(self.scenes.keys()):
                self.scene_combo.addItem(name)
            if current in self.scenes:
                self.scene_combo.setCurrentText(current)
            self.scene_combo.blockSignals(False)
        self._refresh_scene_list_widget()

    def _refresh_scene_list_widget(self):
        if not hasattr(self.list_manager, "scenes_list_widget"):
            return
        selected_name = ""
        selected = self.list_manager.scenes_list_widget.selectedItems()
        if selected:
            selected_name = selected[0].text()
        self.list_manager.scenes_list_widget.clear()
        for name in sorted(self.scenes.keys()):
            self.list_manager.scenes_list_widget.addItem(QListWidgetItem(name))
        if selected_name:
            matches = self.list_manager.scenes_list_widget.findItems(selected_name, Qt.MatchFlag.MatchExactly)
            if matches:
                self.list_manager.scenes_list_widget.setCurrentItem(matches[0])

    def save_scene_prompt(self):
        name, ok = QInputDialog.getText(self, tr("UI", "Save scene"), tr("UI", "Scene name"))
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        self.scenes[name] = self._scene_payload()
        self._refresh_scene_combo()
        self.scene_combo.setCurrentText(name)
        matches = self.list_manager.scenes_list_widget.findItems(name, Qt.MatchFlag.MatchExactly)
        if matches:
            self.list_manager.scenes_list_widget.setCurrentItem(matches[0])
        self.status_label.setText(tr("UI", f"Saved scene: {name}"))

    def delete_scene_prompt(self):
        if not self.scenes:
            return
        name = self.scene_combo.currentText().strip() if self.scene_combo else ""
        if not name:
            name, ok = QInputDialog.getText(self, tr("UI", "Delete scene"), tr("UI", "Scene name"))
            if not ok:
                return
            name = name.strip()
        if not name:
            return
        if name in self.scenes:
            self.scenes.pop(name, None)
            self._refresh_scene_combo()
            self.status_label.setText(tr("UI", f"Deleted scene: {name}"))

    def play_selected_scene_from_list(self):
        selected = self.list_manager.scenes_list_widget.selectedItems()
        if not selected:
            return
        name = selected[0].text().strip()
        if name:
            self.apply_scene_by_name(name)

    def delete_selected_scene_from_list(self):
        selected = self.list_manager.scenes_list_widget.selectedItems()
        if not selected:
            return
        name = selected[0].text().strip()
        if not name:
            return
        self.scenes.pop(name, None)
        self._refresh_scene_combo()
        self.status_label.setText(tr("UI", f"Deleted scene: {name}"))

    def apply_scene_by_name(self, name: str):
        name = name.strip()
        if not name or name not in self.scenes:
            return
        scene = self.scenes[name]
        self.settings.layout_mode = scene.get("layout_mode", self.settings.layout_mode)
        self.layout_combo.setCurrentText(self.settings.layout_mode)
        self.settings.audio_policy = scene.get("audio_policy", self.settings.audio_policy)
        self.active_volume = int(scene.get("active_volume", self.active_volume))
        self.volume_slider.setValue(self.active_volume)

        self._is_clearing = True
        for w in self.video_widgets[:]:
            try:
                w.stop()
            except Exception:
                pass
            w.setParent(None)
            w.deleteLater()
        self.video_widgets.clear()
        self._is_clearing = False
        for item in scene.get("videos", []):
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                url = item[0]
                title = item[1]
                profile = item[2] if len(item) > 2 else None
                self.create_video_widget(url, title, profile)
        self.update_grid()
        self._enforce_audio_policy_with_retries()
        self.status_label.setText(tr("UI", f"Loaded scene: {name}"))

    def on_list_manager_visibility_changed(self, visible: bool):
        if self.settings.show_list_manager != visible:
            self.settings.show_list_manager = visible
            self.sm.save()

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
            raw_feeds = json.loads(feeds_file.read_text(encoding="utf-8"))
        except Exception:
            raw_feeds = {
                "Associated Press": default_feed_profile("https://www.youtube.com/channel/UC52X5wxOL_s5ywGvmcU7v8g"),
                "Reuters": default_feed_profile("https://www.youtube.com/channel/UChqUTb7kYRx8-EiaN3XFrSQ"),
                "CNN": default_feed_profile("https://www.youtube.com/@CNN"),
            }
            try:
                feeds_file.write_text(json.dumps(raw_feeds, indent=4), encoding="utf-8")
            except Exception:
                pass
        feeds: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw_feeds, dict):
            for name, record in raw_feeds.items():
                feeds[str(name)] = normalize_feed_record(record)
        self.news_feeds = feeds
        self._refresh_feeds_list()

    def _refresh_feeds_list(self):
        w = self.list_manager.news_feed_list_widget
        w.clear()
        for name, record in self.news_feeds.items():
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, normalize_feed_record(record))
            w.addItem(item)
        if not self.news_feeds:
            self.status_label.setText(tr("UI", "No saved feeds yet"))

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
            feed_record = normalize_feed_record(item.data(Qt.ItemDataRole.UserRole))
            feed_url = feed_record.get("url", "").strip()
            if feed_url:
                self._enqueue(feed_url, feed_name, feed_record)

    def add_all_feeds(self):
        for name, record in self.news_feeds.items():
            feed_record = normalize_feed_record(record)
            if feed_record.get("url", "").strip():
                self._enqueue(feed_record["url"], name, feed_record)

    def add_new_feed(self):
        dialog = FeedDialog(self, show_profile=True)
        if dialog.exec():
            name, _url = dialog.get_feed_data()
            name = name.strip()
            profile = normalize_feed_record(dialog.get_profile_data())
            if name and profile["url"]:
                self.news_feeds[name] = profile
                self.save_news_feeds()
                self._refresh_feeds_list()

    def edit_feed(self):
        selected = self.list_manager.news_feed_list_widget.selectedItems()
        if not selected:
            return
        item = selected[0]
        old_name = item.text()
        old_record = normalize_feed_record(item.data(Qt.ItemDataRole.UserRole))
        dialog = FeedDialog(self, name=old_name, url=old_record.get("url", ""), profile=old_record, show_profile=True)
        if dialog.exec():
            new_name, _new_url = dialog.get_feed_data()
            new_name = new_name.strip()
            new_record = normalize_feed_record(dialog.get_profile_data())
            if new_name and new_record["url"]:
                self.news_feeds.pop(old_name, None)
                self.news_feeds[new_name] = new_record
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
        if not self.playlists and not self.video_widgets:
            self.status_label.setText(tr("UI", "No playlists saved"))

    def save_playlists(self):
        _, f, _, _ = default_files()
        f.parent.mkdir(parents=True, exist_ok=True)
        try:
            f.write_text(json.dumps(self.playlists, indent=4), encoding="utf-8")
        except Exception:
            pass

    def add_playlist_url(self):
        dialog = FeedDialog(self, name="", url="", show_profile=False)
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

    def _enqueue(self, url, title, feed_profile: Optional[Dict[str, Any]] = None):
        self._queue.append((url, title, feed_profile))
        if not self._queue_timer.isActive():
            self._queue_timer.start()

    def _process_queue(self):
        if not self._queue:
            self._queue_timer.stop()
            return
        batch_size = min(4, len(self._queue))
        for _ in range(batch_size):
            url, title, feed_profile = self._queue.popleft()
            self.create_video_widget(url, title, feed_profile, refresh_grid=False, enforce_audio=False)
            if not self._queue:
                break
        self.update_grid()
        self._enforce_audio_policy_with_retries()
        if not self._queue:
            self._queue_timer.stop()

    def create_video_widget(
            self,
            url,
            title,
            feed_profile: Optional[Dict[str, Any]] = None,
            *,
            refresh_grid: bool = True,
            enforce_audio: bool = True,
    ):
        is_first_video = not bool(self.video_widgets)
        profile = normalize_feed_record(feed_profile or {"url": url})
        default_muted = bool(profile.get("start_muted", True))
        muted = default_muted if not is_first_video else False
        vw = QtTile(url, title, self.settings, parent=self.central_widget_container, muted=muted, feed_profile=profile)
        vw.requestToggle.connect(self.toggle_mute_single)
        vw.started.connect(self.on_tile_playing)
        vw.requestFullscreen.connect(self.toggle_fullscreen_tile)
        vw.requestPip.connect(self.toggle_pip)
        vw.requestRemove.connect(self.remove_video_widget)
        vw.requestActive.connect(self.activate_tile_audio)
        vw.requestReorder.connect(self.reorder_video_widget_pair)
        vw.titleChanged.connect(self.on_tile_title_changed)
        vw.healthChanged.connect(self.on_tile_health_changed)

        self.video_widgets.append(vw)

        if is_first_video:
            self.currently_unmuted = vw
            self.allow_auto_select = True
            self.focused_tile = vw
        elif not muted and self.settings.audio_policy == "single":
            self.currently_unmuted = vw

        if refresh_grid:
            self.update_grid()
        if enforce_audio:
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
        if widget in self.pip_windows:
            pip_window = self.pip_windows.pop(widget)
            pip_window.should_reattach = False
            pip_window.close()
        if self.fullscreen_tile is widget and self.isFullScreen():
            self.exit_fullscreen()
        if widget in self.video_widgets:
            was_unmuted = widget == self.currently_unmuted
            was_focused = widget == self.focused_tile
            if was_unmuted:
                self.currently_unmuted = None
            if was_focused:
                self.focused_tile = None

            safe_disconnect(widget.requestToggle)
            safe_disconnect(widget.started)
            safe_disconnect(widget.requestFullscreen)
            safe_disconnect(widget.requestPip)
            safe_disconnect(widget.requestRemove)
            safe_disconnect(widget.requestActive)
            safe_disconnect(widget.requestReorder)
            safe_disconnect(widget.titleChanged)
            safe_disconnect(widget.healthChanged)

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
            if was_focused and self.video_widgets:
                self.focused_tile = self.video_widgets[0]
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
        self.focused_tile = None

        for tile, pip_window in list(self.pip_windows.items()):
            pip_window.should_reattach = False
            pip_window.close()
        self.pip_windows.clear()

        for w in self.video_widgets[:]:
            safe_disconnect(w.requestToggle)
            safe_disconnect(w.started)
            safe_disconnect(w.requestFullscreen)
            safe_disconnect(w.requestPip)
            safe_disconnect(w.requestRemove)
            safe_disconnect(w.requestActive)
            safe_disconnect(w.requestReorder)
            safe_disconnect(w.titleChanged)
            safe_disconnect(w.healthChanged)
            try:
                w.stop()
            except Exception:
                pass
            w.setParent(None)
            w.deleteLater()

        self.video_widgets.clear()
        self._last_grid_signature = None
        self._grid_list_signature = None

        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            wid = item.widget()
            if wid is not None:
                wid.setParent(None)

        self.list_manager.grid_list_widget.clear()
        self.grid_empty_state.show()
        self.grid_layout.addWidget(self.grid_empty_state, 0, 0, 1, 1, Qt.AlignmentFlag.AlignCenter)

        self._audio_enforce_generation += 1
        self._audio_retry_timer.stop()
        self._audio_retry_remaining = 0

        QTimer.singleShot(0, lambda: setattr(self, "_is_clearing", False))

    def _grid_signature(self, active_widgets: list[QtTile], mode: str) -> Tuple[Any, ...]:
        return mode, tuple(id(w) for w in active_widgets)

    def _grid_list_state(self) -> Tuple[Any, ...]:
        return tuple((id(w), w.title) for w in self.video_widgets)

    def _sync_grid_list(self) -> None:
        signature = self._grid_list_state()
        if signature == self._grid_list_signature:
            return
        w = self.list_manager.grid_list_widget
        w.setUpdatesEnabled(False)
        try:
            w.clear()
            for tile in self.video_widgets:
                item = QListWidgetItem(tile.title)
                item.setData(Qt.ItemDataRole.UserRole, tile)
                w.addItem(item)
        finally:
            w.setUpdatesEnabled(True)
        self._grid_list_signature = signature

    def update_grid(self, force: bool = False):
        active_widgets = [w for w in self.video_widgets if w not in self.pip_windows]
        n_active = len(active_widgets)

        if n_active == 0:
            self._last_grid_signature = None
            self._grid_list_signature = None
            self.list_manager.grid_list_widget.clear()
            self.grid_empty_state.show()
            self.grid_layout.addWidget(self.grid_empty_state, 0, 0, 1, 1, Qt.AlignmentFlag.AlignCenter)
            return

        self.grid_empty_state.hide()

        mode = getattr(self.settings, "layout_mode", "auto")
        if mode == "2x2":
            cols = 2
            rows = math.ceil(n_active / cols)
        elif mode == "3x3":
            cols = 3
            rows = math.ceil(n_active / cols)
        elif mode == "4x4":
            cols = 4
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

        layout_signature = self._grid_signature(active_widgets, mode)
        if not force and layout_signature == self._last_grid_signature:
            self._sync_grid_list()
            return

        self.central_widget_container.setUpdatesEnabled(False)
        try:
            while self.grid_layout.count():
                item = self.grid_layout.takeAt(0)
                _widget = item.widget()

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
                w.set_overlay_visibility(self.settings.show_operational_overlays)
                w.show()
        finally:
            self.central_widget_container.setUpdatesEnabled(True)

        self._last_grid_signature = layout_signature
        self._sync_grid_list()

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
        self.focused_tile = video_widget
        pref_vol = int(video_widget.feed_profile.get("preferred_volume", self.active_volume))
        self.active_volume = max(0, min(100, pref_vol))
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(self.active_volume)
        self.volume_label.setText(tr("UI", f"Volume {self.active_volume}"))
        self.volume_slider.blockSignals(False)

        if not self.settings.audio_follow_selection:
            self._update_active_tile_styles()
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
        self._grid_list_signature = None
        self._sync_grid_list()

    def on_tile_health_changed(self, tile: QtTile):
        if tile.health_state == "error":
            self.logger.warning("Tile error: %s (%s)", tile.title, tile.last_error)
        tile.set_overlay_visibility(self.settings.show_operational_overlays)

    def _run_watchdog(self):
        if not self.settings.watchdog_enabled:
            return
        now = datetime.now()
        threshold = max(5, int(self.settings.watchdog_down_seconds))
        for tile in self.video_widgets:
            if tile.down_since is None:
                continue
            down_for = int((now - tile.down_since).total_seconds())
            if down_for < threshold:
                continue
            key = tile.title
            last = self.last_watchdog_alert.get(key)
            if last and (now - last).total_seconds() < threshold:
                continue
            self.last_watchdog_alert[key] = now
            msg = tr("UI", f"Watchdog: {tile.title} down for {down_for}s")
            self.status_label.setText(msg)
            self.logger.warning(msg)

    def _update_active_tile_styles(self):
        for tile in self.video_widgets:
            tile.set_highlight_state(
                tile is self.currently_unmuted and self.settings.audio_policy == "single",
                tile is self.focused_tile,
            )

    def _fade_tile_volume(self, tile: QtTile, target_percent: int):
        duration = int(self.settings.audio_fade_ms)
        target = max(0.0, min(1.0, float(target_percent) / 100.0))
        if duration <= 0:
            try:
                tile.audio.setVolume(target)
            except Exception:
                pass
            return
        start = tile.audio.volume()
        steps = 6
        interval = max(16, int(duration / steps))
        for i in range(1, steps + 1):
            ratio = i / float(steps)
            vol = start + ((target - start) * ratio)
            QTimer.singleShot(interval * i, lambda v=vol, t=tile: self._safe_set_tile_volume(t, v))

    @staticmethod
    def _safe_set_tile_volume(tile: QtTile, value: float):
        try:
            tile.audio.setVolume(value)
        except Exception:
            pass

    @staticmethod
    def _safe_hard_mute(tile: QtTile):
        try:
            tile.hard_mute()
        except Exception:
            pass

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
                tile.audio.setMuted(False)
                self._fade_tile_volume(tile, 0)
                QTimer.singleShot(max(40, int(self.settings.audio_fade_ms)), lambda t=tile: self._safe_hard_mute(t))
        if active:
            active.make_active(self.active_volume)
            self._fade_tile_volume(active, self.active_volume)
            self.status_label.setText(tr("UI", f"Active: {active.title}"))
        else:
            self.status_label.setText(tr("UI", "No active audio"))
        self._update_active_tile_styles()

    def _enforce_audio_policy_with_retries(self):
        self._audio_enforce_generation += 1
        gen = self._audio_enforce_generation
        self._enforce_audio_policy(gen)
        self._audio_retry_generation = gen
        self._audio_retry_remaining = self.AUDIO_RETRY_COUNT
        if self._audio_retry_remaining > 0:
            self._audio_retry_timer.start()

    def _run_audio_retry(self):
        if self._audio_retry_remaining <= 0:
            self._audio_retry_timer.stop()
            return
        self._enforce_audio_policy(self._audio_retry_generation)
        self._audio_retry_remaining -= 1
        if self._audio_retry_remaining <= 0:
            self._audio_retry_timer.stop()

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

            self.menuBar().hide()
            self.statusBar().hide()
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

        self.menuBar().show()
        self.statusBar().show()
        if self.main_toolbar:
            self.main_toolbar.show()

        for w in self.video_widgets:
            w.show()
            if self.settings.pause_others_in_fullscreen and w is not fs_tile:
                try:
                    w.player.play()
                except Exception:
                    pass

        self.update_grid(force=True)
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
            self.menuBar().hide()
            self.statusBar().hide()
            if self.main_toolbar:
                self.main_toolbar.hide()
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
        self._grid_list_signature = None
        self.update_grid()

    def reorder_video_widget_pair(self, source: QtTile, target: QtTile):
        if source not in self.video_widgets or target not in self.video_widgets or source is target:
            return
        source_index = self.video_widgets.index(source)
        target_index = self.video_widgets.index(target)
        widget = self.video_widgets.pop(source_index)
        self.video_widgets.insert(target_index, widget)
        self.focused_tile = widget
        self.update_grid()
        self._update_active_tile_styles()

    def reload_all_videos(self):
        for widget in self.video_widgets:
            widget.safe_reload()

    def save_state(self):
        _, _, state_file, _ = default_files()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "videos": [(w.url, w.title, w.feed_profile) for w in self.video_widgets],
            "active_volume": int(self.active_volume),
            "geometry": self.saveGeometry().toBase64().data().decode("utf-8"),
            "is_maximized": self.isMaximized(),
            "scenes": self.scenes,
            "focused_title": self.focused_tile.title if self.focused_tile else "",
        }
        try:
            state_file.write_text(json.dumps(state, indent=4), encoding="utf-8")
        except Exception:
            pass

    def load_state(self):
        _, _, state_file, _ = default_files()
        self._refresh_scene_combo()
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

            raw_scenes = state.get("scenes", {})
            self.scenes = raw_scenes if isinstance(raw_scenes, dict) else {}
            self._refresh_scene_combo()

            for rec in state.get("videos", []):
                if isinstance(rec, (list, tuple)) and len(rec) >= 2:
                    url = rec[0]
                    title = rec[1]
                    feed_profile = rec[2] if len(rec) > 2 else None
                    self._enqueue(url, title, feed_profile)
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

    def _palette_actions(self) -> list[Tuple[str, Any]]:
        actions: list[Tuple[str, Any]] = [
            (tr("UI", "Add video from URL"), self.add_video_from_input),
            (tr("UI", "Add all feeds"), self.add_all_feeds),
            (tr("UI", "Mute all tiles"), self.mute_all_tiles),
            (tr("UI", "Next active audio"), self.cycle_active_audio),
            (tr("UI", "Reload all videos"), self.reload_all_videos),
            (tr("UI", "Toggle fullscreen"), self.toggle_grid_fullscreen),
            (tr("UI", "Open settings"), self.open_settings),
            (tr("UI", "Open diagnostics"), self.open_diagnostics),
            (tr("UI", "Save scene"), self.save_scene_prompt),
        ]
        for feed_name in self.news_feeds.keys():
            actions.append((f"{tr('UI', 'Add feed')}: {feed_name}", lambda n=feed_name: self._add_named_feed(n)))
        for scene_name in self.scenes.keys():
            actions.append(
                (f"{tr('UI', 'Load scene')}: {scene_name}", lambda n=scene_name: self.apply_scene_by_name(n)))
        return actions

    def _add_named_feed(self, feed_name: str):
        rec = self.news_feeds.get(feed_name)
        if not rec:
            return
        profile = normalize_feed_record(rec)
        if profile["url"]:
            self._enqueue(profile["url"], feed_name, profile)

    def open_command_palette(self):
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("UI", "Command Palette"))
        dlg.setMinimumSize(520, 420)
        lay = QVBoxLayout(dlg)
        search = QLineEdit()
        search.setPlaceholderText(tr("UI", "Type a command, feed, or scene"))
        items = QListWidget()
        lay.addWidget(search)
        lay.addWidget(items)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        lay.addWidget(buttons)

        all_actions = self._palette_actions()

        def refill(text: str):
            q = text.lower().strip()
            items.clear()
            for label, cb in all_actions:
                if q and q not in label.lower():
                    continue
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, cb)
                items.addItem(item)
            if items.count() > 0:
                items.setCurrentRow(0)

        def execute_selected():
            item = items.currentItem()
            if not item:
                return
            cb = item.data(Qt.ItemDataRole.UserRole)
            if callable(cb):
                cb()
            dlg.accept()

        search.textChanged.connect(refill)
        search.returnPressed.connect(execute_selected)
        items.itemDoubleClicked.connect(lambda _item: execute_selected())
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        refill("")
        search.setFocus()
        dlg.exec()

    def open_settings(self):
        dlg = SettingsDialog(self, self.sm)
        result = dlg.exec()

        if dlg.profile_imported:
            configure_logging(self.sm.logger, self.sm.settings.verbose_logging)
            self.refresh_ui()
            QMessageBox.information(self, APP_NAME, tr("Settings", "Profile imported and applied."))
        elif result:
            dlg.apply()
            self.settings = self.sm.settings
            self.apply_theme()
            self._apply_ui_density()
            self.list_manager.setVisible(self.settings.show_list_manager)
            for tile in self.video_widgets:
                tile._settings = self.settings
                tile.set_overlay_visibility(self.settings.show_operational_overlays)
            self._update_active_tile_styles()
            self._enforce_audio_policy_with_retries()
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
            + "Ctrl+K: "
            + tr("Help", "Open command palette")
            + "\n"
            + "Alt+Left/Alt+Right: "
            + tr("Help", "Move focused tile in grid order")
            + "\n"
            + "Ctrl+1..Ctrl+6: "
            + tr("Help", "Switch layout presets")
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
    sm = SettingsManager(logger)
    sm.load()
    configure_logging(logger, sm.settings.verbose_logging)

    def qt_message_handler(msg_type, _msg_log_context, msg_string):
        try:
            if sm.settings.verbose_logging:
                logger.info("%s: %s", msg_type.name, msg_string)
                return
            if msg_type.name in ("QtWarningMsg",):
                logger.warning("%s: %s", msg_type.name, msg_string)
            elif msg_type.name in ("QtCriticalMsg", "QtFatalMsg"):
                logger.error("%s: %s", msg_type.name, msg_string)
        except Exception:
            pass

    qInstallMessageHandler(qt_message_handler)

    app = QApplication(sys.argv)
    app.setApplicationDisplayName(APP_NAME)

    win = NewsBoard(sm)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
