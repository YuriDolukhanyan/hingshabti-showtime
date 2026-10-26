"""Minimal local presenter: a fullscreen output window plus a control window with a 0-150% volume control.

Usage: python presenter.py "C:\\path\\to\\folder"
"""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QUrl, Signal
from PySide6.QtGui import QGuiApplication, QImage, QKeySequence, QPalette, QPixmap, QShortcut, QWindow
from PySide6.QtMultimedia import QAudioBufferOutput, QAudioFormat, QAudioSink, QMediaDevices, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QSizePolicy,
                               QSlider, QStackedWidget, QVBoxLayout, QWidget)

EXTENSIONS = {".mp4", ".png", ".jpg", ".jpeg"}
VIDEO_EXTENSIONS = {".mp4"}
BLACK = None  # item that shows an empty black screen (right before and right after the numbered media)
INTRO_PREFIX = "start_"  # start_1.png, start_2.png, ... come first and are not numbered
OUTRO_PREFIX = "end_"  # end_1.png, end_2.png, ... come last and are not numbered
MAX_VOLUME = 200  # percent
VOLUME_STEP = 5  # percent per Up / Down key press
NUMBER_MARGIN = 20  # px between the media number and the output's upper-left corner
RESTART_AFTER_MS = 2000  # Left on a video played longer than this restarts it instead of going back


def natural_key(path):
    """'2.png' < '10.png': split the name into text and number parts and compare numbers as ints."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def load_files(folder):
    """Returns (intro, media, outro): the start_* slides, the numbered media and the end_* slides, each sorted."""
    files = sorted((p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS),
                   key=natural_key)
    intro = [p for p in files if p.name.lower().startswith(INTRO_PREFIX)]
    outro = [p for p in files if p.name.lower().startswith(OUTRO_PREFIX)]
    media = [p for p in files if p not in intro and p not in outro]
    return intro, media, outro


def is_video(item):
    return item is not BLACK and item.suffix.lower() in VIDEO_EXTENSIONS


def is_image(item):
    return item is not BLACK and not is_video(item)


def format_time(ms):
    seconds = ms // 1000
    return f"{seconds // 60}:{seconds % 60:02}"


class ImageLabel(QLabel):
    """QLabel that keeps the original pixmap and rescales it to fit (keeping aspect ratio) on every resize."""

    def __init__(self):
        super().__init__()
        self.original = QPixmap()
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

    def set_image(self, pixmap):
        self.original = pixmap
        self.rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.rescale()

    def rescale(self):
        if self.original.isNull():
            self.clear()  # nothing to draw: the black window background shows through
            return
        dpr = self.devicePixelRatioF()  # scale in device pixels so images stay sharp on HiDPI screens
        scaled = self.original.scaled(self.size() * dpr, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self.setPixmap(scaled)


class OutputWindow(QWidget):
    """Fullscreen black window with no UI: shows either the video widget or the image label."""

    def __init__(self):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setWindowTitle("Presenter Output")
        palette = self.palette()
        palette.setColor(QPalette.Window, Qt.black)
        self.setPalette(palette)
        self.setAutoFillBackground(True)

        self.video = QVideoWidget()
        self.image = ImageLabel()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.image)
        self.stack.addWidget(self.video)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

        for widget in (self, self.stack, self.video, self.image):
            widget.setCursor(Qt.BlankCursor)
            widget.setFocusPolicy(Qt.NoFocus)

        self.number = NumberOverlay(self)

    def show_on(self, screen):
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.showFullScreen()


class NumberOverlay(QLabel):
    """See-through window that shows the media number above the output's upper-left corner.

    The video is drawn in its own native window, so a normal child label would be hidden behind it.
    Being owned by the output keeps it on top of the output; clicks pass through to the output.
    """

    def __init__(self, owner):
        super().__init__(owner, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
                         | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet("color: white; font-family: Arial; font-size: 115pt; font-weight: bold;")

    def show_number(self, number):
        self.setText(str(number))
        self.adjustSize()
        self.move(self.parentWidget().geometry().topLeft() + QPoint(NUMBER_MARGIN, NUMBER_MARGIN))
        self.show()


class BoostedAudio(QObject):
    """Plays the player's audio through a QAudioSink with a gain of 0..1.5.

    QAudioOutput can't go above 100%, so the player sends its audio here instead and numpy applies the gain.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        device = QMediaDevices.defaultAudioOutput()
        audio_format = device.preferredFormat()
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Float)
        self.sink = QAudioSink(device, audio_format, self)
        self.buffer_output = QAudioBufferOutput(audio_format, self)  # the player converts its audio to this format
        self.buffer_output.audioBufferReceived.connect(self.play_buffer)
        self.io = None
        self.gain = 1.0

    def play_buffer(self, buffer):
        if buffer.byteCount() == 0:  # the player sends empty buffers, e.g. at the end of a video
            return
        if self.io is None:
            self.io = self.sink.start()
        samples = np.frombuffer(buffer.constData(), dtype=np.float32) * self.gain
        self.io.write(np.clip(samples, -1.0, 1.0).astype(np.float32).tobytes())  # clip so loud parts don't wrap

    def stop(self):
        self.sink.stop()  # drops audio that is still queued
        self.io = None


class Timeline(QProgressBar):
    """Progress bar of the current video; while clickable, click (or drag) on it to jump to that time."""

    seek_requested = Signal(int)  # position in ms

    def __init__(self):
        super().__init__()
        self.setTextVisible(False)
        self.clickable = False

    def set_clickable(self, clickable):
        self.clickable = clickable
        self.setCursor(Qt.PointingHandCursor if clickable else Qt.ArrowCursor)

    def mousePressEvent(self, event):
        if self.clickable and event.button() == Qt.LeftButton:
            self.seek_to(event.position().x())

    def mouseMoveEvent(self, event):
        if self.clickable and event.buttons() & Qt.LeftButton:
            self.seek_to(event.position().x())

    def seek_to(self, x):
        fraction = min(max(x / max(self.width(), 1), 0.0), 1.0)
        self.seek_requested.emit(round(fraction * self.maximum()))


class ControlWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Presenter Control")
        self.resize(900, 300)
        self.current_label = QLabel()
        self.current_label.setStyleSheet("font-size: 22pt; font-weight: bold;")
        self.next_label = QLabel()
        self.next_label.setStyleSheet("font-size: 14pt; color: gray;")

        self.volume_label = QLabel()
        self.volume_label.setStyleSheet("font-size: 16pt;")
        self.volume_label.setMinimumWidth(180)
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, MAX_VOLUME)
        self.volume_slider.setPageStep(10)
        self.volume_slider.setTickInterval(25)
        self.volume_slider.setTickPosition(QSlider.TicksBelow)
        volume_row = QHBoxLayout()
        volume_row.addWidget(self.volume_label)
        volume_row.addWidget(self.volume_slider, 1)

        self.time_label = QLabel()
        self.time_label.setStyleSheet("font-size: 16pt;")
        self.time_label.setMinimumWidth(180)
        self.timeline = Timeline()
        time_row = QHBoxLayout()
        time_row.addWidget(self.time_label)
        time_row.addWidget(self.timeline, 1)

        self.number_button = QPushButton("Show number on screen")
        self.number_button.setCheckable(True)
        self.number_button.setStyleSheet("font-size: 12pt; padding: 6px;")
        self.seek_button = QPushButton("Clickable timeline")
        self.seek_button.setCheckable(True)  # off by default, so a stray click can't jump in the video
        self.seek_button.setStyleSheet("font-size: 12pt; padding: 6px;")
        self.seek_button.toggled.connect(self.timeline.set_clickable)
        button_row = QHBoxLayout()
        button_row.addWidget(self.number_button)
        button_row.addWidget(self.seek_button)
        button_row.addStretch(1)

        hint = QLabel("Right = next    Left = previous (restarts a video after 2 s)    Up / Down = volume    "
                      "Esc = quit    "
                      "Space / click on output = pause / continue video    "
                      "Click on timeline = jump (if clickable)")
        hint.setStyleSheet("color: gray;")

        layout = QVBoxLayout(self)
        layout.addWidget(self.current_label)
        layout.addWidget(self.next_label)
        layout.addLayout(button_row)
        layout.addStretch(1)
        layout.addLayout(time_row)
        layout.addLayout(volume_row)
        layout.addWidget(hint)
        for widget in (self, self.current_label, self.next_label, self.time_label, self.timeline, self.volume_label,
                       self.volume_slider, self.number_button, self.seek_button, hint):
            widget.setFocusPolicy(Qt.NoFocus)  # mouse still works, but these never eat arrow keys

    def closeEvent(self, event):
        QApplication.quit()  # closing the control window ends the whole show
        super().closeEvent(event)


class ImageLoader(QObject):
    """Decodes images on a worker thread; the QPixmap itself is created on the GUI thread."""

    loaded = Signal(int, QImage)

    def __init__(self):
        super().__init__()
        self.pool = ThreadPoolExecutor(max_workers=1)

    def request(self, index, path):
        self.pool.submit(lambda: self.loaded.emit(index, QImage(str(path))))


class Presenter(QObject):
    """Owns the item list and current index and updates both windows directly."""

    def __init__(self, intro, media, outro, app):
        super().__init__()
        self.intro, self.media, self.outro = intro, media, outro
        self.items = [*intro, BLACK, *media, BLACK, *outro]
        self.first_media = len(intro) + 1  # index of media number 1
        self.index = 0
        self.cache = {}  # index -> QPixmap, only for the current item and its neighbours
        self.pending = set()

        self.output = OutputWindow()
        self.control = ControlWindow()

        self.audio = BoostedAudio(self)
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.output.video)
        self.player.setAudioBufferOutput(self.audio.buffer_output)  # no QAudioOutput: BoostedAudio plays the sound
        self.player.positionChanged.connect(self.update_timeline)
        self.player.durationChanged.connect(self.update_timeline)
        self.control.timeline.seek_requested.connect(self.seek)

        slider = self.control.volume_slider
        slider.valueChanged.connect(self.set_volume)
        slider.setValue(100)
        self.set_volume(slider.value())
        self.control.number_button.toggled.connect(self.update_number)

        self.loader = ImageLoader()
        self.loader.loaded.connect(self.on_image_loaded)

        keys = (
            (Qt.Key_Right, self.next),
            (Qt.Key_Left, self.previous),
            (Qt.Key_Up, lambda: slider.setValue(slider.value() + VOLUME_STEP)),
            (Qt.Key_Down, lambda: slider.setValue(slider.value() - VOLUME_STEP)),
            (Qt.Key_Space, self.toggle_pause),
            (Qt.Key_Escape, app.quit),
        )
        for key, slot in keys:
            shortcut = QShortcut(QKeySequence(key), self.control)
            shortcut.setContext(Qt.ApplicationShortcut)  # works whichever window is focused
            shortcut.activated.connect(slot)

        app.installEventFilter(self)  # to catch clicks on the output window
        app.aboutToQuit.connect(self.player.stop)

    def start(self):
        primary = QGuiApplication.primaryScreen()
        others = [s for s in QGuiApplication.screens() if s.name() != primary.name()]
        self.output.show_on(others[0] if others else primary)

        self.control.setScreen(primary)
        self.control.move(primary.availableGeometry().center() - self.control.rect().center())
        self.control.show()
        self.control.raise_()
        self.control.activateWindow()
        self.show_item(0)

    def set_volume(self, percent):
        self.audio.gain = percent / 100
        self.control.volume_label.setText(f"Volume: {percent}%")

    def next(self):
        if self.index + 1 < len(self.items):
            self.show_item(self.index + 1)

    def previous(self):
        if is_video(self.items[self.index]) and self.player.position() > RESTART_AFTER_MS:
            self.seek(0)  # restart the video
        elif self.index > 0:
            self.show_item(self.index - 1)

    def toggle_pause(self):
        if not is_video(self.items[self.index]):
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()  # keep the current frame on screen, just stop playing
        else:
            self.player.play()  # continue from the paused frame (an ended video starts again)

    def seek(self, position):
        if not is_video(self.items[self.index]):
            return
        self.audio.stop()  # drop the queued audio of the old position
        self.player.setPosition(position)
        self.player.play()  # also plays again if it was paused by a click or had ended

    def show_item(self, index):
        self.index = index
        item = self.items[index]
        self.player.stop()
        self.player.setSource(QUrl())
        self.audio.stop()

        if is_video(item):
            self.output.stack.setCurrentWidget(self.output.video)
            self.player.setSource(QUrl.fromLocalFile(str(item)))
            self.player.play()  # plays once (loops defaults to 1) and stays on the last frame
        else:
            self.output.image.set_image(self.pixmap(index))  # an empty pixmap for BLACK
            self.output.stack.setCurrentWidget(self.output.image)

        self.update_info()
        self.update_number()
        self.update_timeline()
        self.preload()

    def pixmap(self, index):
        if not is_image(self.items[index]):
            return QPixmap()
        if index not in self.cache:
            self.cache[index] = QPixmap(str(self.items[index]))  # not preloaded yet: load it now
        return self.cache[index]

    def preload(self):
        wanted = {i for i in (self.index - 1, self.index, self.index + 1) if 0 <= i < len(self.items)}
        self.cache = {i: pixmap for i, pixmap in self.cache.items() if i in wanted}
        for i in sorted(wanted, key=lambda i: i != self.index + 1):  # next item first
            if is_image(self.items[i]) and i not in self.cache and i not in self.pending:
                self.pending.add(i)
                self.loader.request(i, self.items[i])

    def on_image_loaded(self, index, image):
        self.pending.discard(index)
        if abs(index - self.index) <= 1 and index not in self.cache and not image.isNull():
            self.cache[index] = QPixmap.fromImage(image)

    def describe(self, index):
        if index >= len(self.items):
            return "(end)"
        if self.items[index] is BLACK:
            return "black screen"
        return self.items[index].name

    def media_number(self, index):
        """1, 2, ... for the media between the two black screens; None for intro, outro and black screens."""
        number = index - self.first_media + 1
        return number if 1 <= number <= len(self.media) else None

    def update_info(self):
        item, number = self.items[self.index], self.media_number(self.index)
        outro_start = self.first_media + len(self.media) + 1  # index of end_1
        if number is not None:
            current = f"{number} / {len(self.media)} - {item.name}"
        elif item is BLACK:
            current = "Black screen - before media" if self.index < self.first_media else "Black screen - after media"
        elif self.index < self.first_media:
            current = f"Intro {self.index + 1} / {len(self.intro)} - {item.name}"
        else:
            current = f"Outro {self.index - outro_start + 1} / {len(self.outro)} - {item.name}"
        self.control.current_label.setText(current)
        self.control.next_label.setText(f"Next: {self.describe(self.index + 1)}")

    def update_number(self):
        """Media number (1, 2, ...) in the output's upper-left corner, if the toggle is on; only for numbered media."""
        number = self.media_number(self.index)
        if self.control.number_button.isChecked() and number is not None:
            self.output.number.show_number(number)
        else:
            self.output.number.hide()

    def update_timeline(self):
        """Elapsed / total time of the current video on the control window; empty for images and black screens."""
        timeline, label = self.control.timeline, self.control.time_label
        if not is_video(self.items[self.index]):
            timeline.setRange(0, 1)
            timeline.setValue(0)
            label.setText("No video")
            return
        duration, position = self.player.duration(), self.player.position()
        timeline.setRange(0, max(duration, 1))
        timeline.setValue(min(position, duration))
        label.setText(f"{format_time(position)} / {format_time(duration)}")

    def is_output(self, obj):
        window = obj if isinstance(obj, QWindow) else None
        while window is not None:  # the video widget may use its own child QWindow, so walk up the parents
            if window is self.output.windowHandle():
                return True
            window = window.parent()
        return False

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick) and self.is_output(obj):
            self.toggle_pause()
            return True  # clicks on the output do nothing else
        return False


def main():
    app = QApplication(sys.argv)
    folder = sys.argv[1] if len(sys.argv) > 1 else QFileDialog.getExistingDirectory(None, "Choose presentation folder")
    if not folder:
        return 0
    if not Path(folder).is_dir():
        print(f"Not a folder: {folder}")
        return 1
    intro, media, outro = load_files(folder)
    if not (intro or media or outro):
        print(f"No .mp4 / .png / .jpg / .jpeg files in {folder}")
        return 1

    presenter = Presenter(intro, media, outro, app)
    presenter.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
