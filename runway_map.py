"""Small online street map. Only an explicit preview or target submission selects a site."""

import math
from functools import partial
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QStandardPaths, Qt, QUrl
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkRequest
from PySide6.QtWidgets import QWidget


class RunwayMap(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(195)
        self.center = None
        self.preview_location = self.drag = None
        self.zoom = 18
        self.targets = {}
        self.active = ""
        self.network = QNetworkAccessManager(self)
        cache = QNetworkDiskCache(self)
        cache.setCacheDirectory(
            str(
                Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation))
                / "osm"
            )
        )
        cache.setMaximumCacheSize(32 * 1024 * 1024)
        self.network.setCache(cache)
        self.tiles = {}
        self.pending = set()
        self.failed = set()

    def preview(self, lat, lon):
        if not (-85.0511 <= lat <= 85.0511 and -180 <= lon <= 180):
            raise ValueError("The street map covers latitudes within ±85.05°.")
        self.preview_location = self.center = lat, lon
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.failed.clear()
        self.update()

    def pixel(self, lat, lon):
        n = 256 * 2**self.zoom
        angle = math.radians(max(-85.0511, min(85.0511, lat)))
        return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(angle)) / math.pi) * n / 2

    def load(self, key):
        if key in self.pending or key in self.failed or len(self.pending) >= 12:
            return
        z, x, y = key
        request = QNetworkRequest(QUrl(f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"))
        request.setRawHeader(
            b"User-Agent", b"RavenStrike-GS/0.1 (runway debris research ground station)"
        )
        request.setAttribute(
            QNetworkRequest.Attribute.CacheLoadControlAttribute,
            QNetworkRequest.CacheLoadControl.PreferCache,
        )
        request.setTransferTimeout(6000)
        self.pending.add(key)
        reply = self.network.get(request)
        reply.finished.connect(partial(self.received, key, reply))

    def received(self, key, reply):
        self.pending.discard(key)
        tile = QPixmap()
        if tile.loadFromData(reply.readAll(), "PNG"):
            if len(self.tiles) >= 96:
                self.tiles.pop(next(iter(self.tiles)))
            self.tiles[key] = tile
        else:
            self.failed.add(key)
        reply.deleteLater()
        self.update()

    def reset_view(self):
        self.center = self.preview_location = self.drag = None
        self.active = ""
        self.targets.clear()
        self.unsetCursor()
        self.update()

    def change_zoom(self, steps):
        if self.center:
            self.zoom = max(3, min(19, self.zoom + steps))
            self.update()

    def wheelEvent(self, event):
        if event.angleDelta().y():
            self.change_zoom(1 if event.angleDelta().y() > 0 else -1)
        event.accept()

    def mousePressEvent(self, event):
        if self.center and event.button() == Qt.MouseButton.LeftButton:
            self.drag = (event.position(), self.pixel(*self.center))
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.drag:
            start, (x, y) = self.drag
            delta = event.position() - start
            size = 256 * 2**self.zoom
            y = max(0, min(1, (y - delta.y()) / size))
            self.center = (
                math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y)))),
                ((x - delta.x()) / size * 360) % 360 - 180,
            )
            self.update()

    def mouseReleaseEvent(self, event):
        if self.drag and event.button() == Qt.MouseButton.LeftButton:
            self.drag = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#eaf1f0"))
        if self.center is None:
            # A blank plotting grid, with no invented site or robot position.
            p.setPen(QPen(QColor("#d3e1df"), 1))
            for x in range(self.width() // 2 % 32, self.width(), 32):
                p.drawLine(x, 0, x, self.height())
            for y in range(self.height() // 2 % 32, self.height(), 32):
                p.drawLine(0, y, self.width(), y)
            message = "Preview a location to load the map"
            w = p.fontMetrics().horizontalAdvance(message) + 32
            plate = QRect((self.width() - w) // 2, (self.height() - 36) // 2, w, 36)
            p.fillRect(plate, QColor("#ffffff"))
            p.setPen(QColor("#365851"))
            p.drawText(plate, Qt.AlignmentFlag.AlignCenter, message)
            return
        cx, cy = self.pixel(*self.center)
        left, top = round(cx - self.width() / 2), round(cy - self.height() / 2)
        count = 2**self.zoom
        for x in range(left // 256, (left + self.width()) // 256 + 1):
            for y in range(max(0, top // 256), min(count, (top + self.height()) // 256 + 1)):
                key = self.zoom, x % count, y
                if key in self.tiles:
                    p.drawPixmap(x * 256 - left, y * 256 - top, self.tiles[key])
                else:
                    self.load(key)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        def screen_point(location):
            x, y = self.pixel(*location)
            width = count * 256
            x = cx + (x - cx + width / 2) % width - width / 2
            return QPointF(x - left, y - top)

        for name, location in self.targets.items():
            pt = screen_point(location)
            p.setPen(QPen(Qt.GlobalColor.white, 2))
            p.setBrush(QColor("#bb4c15" if name == self.active else "#186784"))
            p.drawEllipse(pt, 6, 6)
            p.setPen(Qt.GlobalColor.black)
            p.drawText(pt + QPointF(9, 4), name)
        p.setPen(QPen(QColor("#252c35"), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.preview_location:
            p.drawEllipse(screen_point(self.preview_location), 9, 9)
        caption = "© OpenStreetMap contributors"
        if self.failed:
            caption = "Map tiles unavailable   ·   " + caption
        width = p.fontMetrics().horizontalAdvance(caption) + 12
        box = QRect(self.width() - width, self.height() - 21, width, 21)
        p.fillRect(box, Qt.GlobalColor.white)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, caption)
