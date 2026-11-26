"""Simple mp3 player: one tab per subfolder, songs played since start-up get a light green background.

Usage:
  python mp3player.py "C:\\path\\to\\music"    every subfolder with .mp3 files becomes a tab
"""
import re
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSlider, QStyle, QTabWidget, QVBoxLayout, QWidget)

AUDIO_EXTENSIONS = {".mp3"}
VOLUME_STEP = 3  # percent per arrow button click
RESTART_AFTER_MS = 2000  # previous button after this much of a song restarts it instead of going back
PLAYED_COLOR = "#c8f7c5"  # light green: played since the program started
NORMAL_COLOR = "white"
ICON_COLOR = "#333333"
PAUSED_COLOR = "#0067c0"  # pause button while the song is paused (the same blue as the toggled buttons)
MEDIA_BUTTON_STYLE = ("QPushButton {{ background: {background}; border: 1px solid {border}; border-radius: 10px; }}"
                      "QPushButton:hover {{ border-color: {PAUSED_COLOR}; }}")


def natural_key(path):
    """'2.mp3' < '10.mp3': split the name into text and number parts and compare numbers as ints."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def load_playlists(folder):
    """[(tab name, sorted .mp3 paths)] for every subfolder that has .mp3 files."""
    playlists = []
    for sub in sorted((p for p in Path(folder).iterdir() if p.is_dir()), key=natural_key):
        songs = sorted((p for p in sub.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS),
                       key=natural_key)
        if songs:
            playlists.append((sub.name, songs))
    return playlists


def format_time(ms):
    seconds = ms // 1000
    return f"{seconds // 60}:{seconds % 60:02}"


def media_icon(kind, color):
    """Smooth play triangle or two rounded pause bars, drawn large so they stay sharp when scaled down."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    if kind == "pause":
        painter.drawRoundedRect(QRectF(14, 8, 13, 48), 4, 4)
        painter.drawRoundedRect(QRectF(37, 8, 13, 48), 4, 4)
    elif kind == "next":  # triangle with a bar on the right
        painter.drawPolygon(QPolygonF([QPointF(10, 8), QPointF(44, 32), QPointF(10, 56)]))
        painter.drawRoundedRect(QRectF(45, 8, 10, 48), 3, 3)
    elif kind == "previous":  # bar on the left with a triangle
        painter.drawRoundedRect(QRectF(9, 8, 10, 48), 3, 3)
        painter.drawPolygon(QPolygonF([QPointF(54, 8), QPointF(20, 32), QPointF(54, 56)]))
    else:
        painter.drawPolygon(QPolygonF([QPointF(16, 6), QPointF(56, 32), QPointF(16, 58)]))
    painter.end()
    return QIcon(pixmap)


def style_media_button(button, kind, highlighted=False):
    """Rounded white button with a dark icon; blue with a white icon when highlighted."""
    background = PAUSED_COLOR if highlighted else "white"
    border = PAUSED_COLOR if highlighted else "#c8c8c8"
    button.setStyleSheet(MEDIA_BUTTON_STYLE.format(background=background, border=border, PAUSED_COLOR=PAUSED_COLOR))
    button.setIcon(media_icon(kind, "white" if highlighted else ICON_COLOR))


class ClickSlider(QSlider):
    """Slider that jumps straight to where you click (or drag) on it."""

    seek = Signal(int)  # value chosen with the mouse

    def __init__(self, orientation):
        super().__init__(orientation)
        self.dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.jump_to(event.position())

    def mouseMoveEvent(self, event):
        if self.dragging:
            self.jump_to(event.position())

    def mouseReleaseEvent(self, event):
        self.dragging = False

    def jump_to(self, pos):
        if self.orientation() == Qt.Horizontal:
            value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), round(pos.x()), self.width())
        else:  # vertical: the bottom is the minimum
            value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), round(pos.y()), self.height(),
                                                   True)
        self.setValue(value)
        self.seek.emit(value)


class SongRow(QFrame):
    """One song box: its own play button and the numbered file name."""

    def __init__(self, number, path):
        super().__init__()
        self.setObjectName("song")
        self.play_button = QPushButton("▶")
        self.play_button.setFixedSize(40, 32)
        label = QLabel(f"{number}. {path.name}")
        label.setStyleSheet("font-size: 12pt;")
        layout = QHBoxLayout(self)
        layout.addWidget(self.play_button)
        layout.addWidget(label, 1)
        self.set_played(False)

    def set_played(self, played):
        color = PLAYED_COLOR if played else NORMAL_COLOR
        self.setStyleSheet(f"QFrame#song {{ background: {color}; border: 1px solid gray; border-radius: 4px; }}")


class MusicPlayer(QWidget):
    def __init__(self, playlists):
        super().__init__()
        self.setWindowTitle("MP3 Հինգշաբթի")
        self.resize(480, 720)
        self.playlists = playlists
        self.rows = []  # per tab: its SongRow boxes
        self.current = None  # (tab, row) of the loaded song

        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)

        # volume: up / down arrows on the left of the vertical bar
        self.volume_slider = ClickSlider(Qt.Vertical)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.valueChanged.connect(self.update_volume)
        up_button = QPushButton("▲")
        up_button.clicked.connect(lambda: self.volume_slider.setValue(self.volume_slider.value() + VOLUME_STEP))
        down_button = QPushButton("▼")
        down_button.clicked.connect(lambda: self.volume_slider.setValue(self.volume_slider.value() - VOLUME_STEP))
        for button in (up_button, down_button):
            button.setFixedSize(64, 64)
            button.setStyleSheet("font-size: 24pt;")
        arrows = QVBoxLayout()
        arrows.addWidget(up_button)
        arrows.addStretch(1)
        arrows.addWidget(down_button)
        volume_row = QHBoxLayout()
        volume_row.addLayout(arrows)
        volume_row.addWidget(self.volume_slider)
        self.volume_label = QLabel()
        self.volume_label.setAlignment(Qt.AlignCenter)
        volume_column = QVBoxLayout()
        volume_column.addLayout(volume_row, 1)
        volume_column.addWidget(self.volume_label)

        # current song: name, play / pause, timeline
        self.name_label = QLabel("No song")
        self.name_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
        self.name_label.setWordWrap(True)
        play_button = QPushButton()
        play_button.clicked.connect(self.play)
        style_media_button(play_button, "play")
        self.pause_button = QPushButton()
        self.pause_button.clicked.connect(self.pause)
        style_media_button(self.pause_button, "pause")
        previous_button = QPushButton()
        previous_button.clicked.connect(self.previous)
        style_media_button(previous_button, "previous")
        next_button = QPushButton()
        next_button.clicked.connect(self.next)
        style_media_button(next_button, "next")
        for button in (previous_button, play_button, self.pause_button, next_button):
            button.setFixedSize(64, 48)
            button.setIconSize(QSize(24, 24))
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setStyleSheet("font-size: 12pt;")
        controls = QHBoxLayout()
        controls.addWidget(previous_button)
        controls.addWidget(play_button)
        controls.addWidget(self.pause_button)
        controls.addWidget(next_button)
        controls.addStretch(1)
        self.timeline = ClickSlider(Qt.Horizontal)
        self.timeline.seek.connect(self.player.setPosition)
        song_column = QVBoxLayout()
        song_column.addWidget(self.name_label)
        song_column.addLayout(controls)
        song_column.addWidget(self.time_label)  # under the buttons, above the timeline
        song_column.addStretch(1)
        song_column.addWidget(self.timeline)

        top = QHBoxLayout()
        top.addLayout(volume_column)
        top.addLayout(song_column, 1)

        # Clear, Auto, Always on top
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.clear_tab)
        self.auto_button = QPushButton("Auto")
        self.auto_button.setCheckable(True)  # on: the next song of the tab starts when one ends
        self.on_top_button = QPushButton("Always on top")
        self.on_top_button.setCheckable(True)
        self.on_top_button.setChecked(True)  # on by default
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)  # the window isn't shown yet, so no show() needed here
        self.on_top_button.toggled.connect(self.set_always_on_top)
        button_row = QHBoxLayout()
        for button in (clear_button, self.auto_button, self.on_top_button):
            button.setStyleSheet("font-size: 12pt; padding: 6px;")
            button_row.addWidget(button)
        button_row.addStretch(1)

        # one tab per subfolder, each a scrollable list of song boxes
        self.tabs = QTabWidget()
        for tab, (name, songs) in enumerate(playlists):
            rows = []
            song_list = QWidget()
            list_layout = QVBoxLayout(song_list)
            for row, path in enumerate(songs):
                box = SongRow(row + 1, path)
                box.play_button.clicked.connect(lambda _checked=False, tab=tab, row=row: self.play_song(tab, row))
                list_layout.addWidget(box)
                rows.append(box)
            list_layout.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(song_list)
            self.tabs.addTab(scroll, name)
            self.rows.append(rows)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(button_row)
        layout.addWidget(self.tabs, 1)

        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(self.timeline.setMaximum)
        self.player.mediaStatusChanged.connect(self.on_status)
        self.player.playbackStateChanged.connect(self.on_playback_state)
        self.update_volume()

    def play_song(self, tab, row):
        """A song box's ▶: start this song from the beginning."""
        self.current = (tab, row)
        path = self.playlists[tab][1][row]
        self.rows[tab][row].set_played(True)
        self.name_label.setText(path.name)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def play(self):
        """Top ▶: continue the loaded song, or start the first song of the open tab."""
        if self.current is None:
            self.play_song(self.tabs.currentIndex(), 0)
        else:
            self.player.play()  # an ended song starts again from the beginning

    def pause(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()

    def next(self):
        """Next song in the list of the current song's tab; after the last song it does nothing."""
        if self.current is None:
            self.play()
            return
        tab, row = self.current
        if row + 1 < len(self.rows[tab]):
            self.play_song(tab, row + 1)

    def previous(self):
        """After 2 s restart the current song, otherwise the previous song (the first song restarts)."""
        if self.current is None:
            self.play()
            return
        tab, row = self.current
        if self.player.position() > RESTART_AFTER_MS or row == 0:
            self.player.setPosition(0)
            self.player.play()
        else:
            self.play_song(tab, row - 1)

    def on_playback_state(self, state):
        style_media_button(self.pause_button, "pause", highlighted=state == QMediaPlayer.PausedState)

    def update_volume(self):
        percent = self.volume_slider.value()
        self.volume_label.setText(f"{percent}%")
        self.audio.setVolume(percent / 100)

    def on_position(self, position):
        if not self.timeline.dragging:
            self.timeline.setValue(position)
        self.time_label.setText(f"{format_time(position)} / {format_time(self.player.duration())}")

    def on_status(self, status):
        if status != QMediaPlayer.EndOfMedia or self.current is None:
            return
        tab, row = self.current
        if self.auto_button.isChecked() and row + 1 < len(self.rows[tab]):
            self.play_song(tab, row + 1)  # Auto: the next song of the same tab; after the last one it stops

    def clear_tab(self):
        """Clear: removes the green marks of the open tab only."""
        for box in self.rows[self.tabs.currentIndex()]:
            box.set_played(False)

    def set_always_on_top(self, on_top):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on_top)
        self.show()  # changing a window flag hides the window, so show it again
        self.activateWindow()


def main():
    app = QApplication(sys.argv)
    folder = sys.argv[1] if len(sys.argv) > 1 else QFileDialog.getExistingDirectory(
        None, "Choose the music folder (one tab per subfolder)")
    if not folder:
        return 0
    if not Path(folder).is_dir():
        print(f"Not a folder: {folder}")
        return 1
    playlists = load_playlists(folder)
    if not playlists:
        print(f"No subfolders with .mp3 files found in: {folder}")
        return 1

    window = MusicPlayer(playlists)
    app.aboutToQuit.connect(window.player.stop)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
