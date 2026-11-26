"""Online mission map with explicit site, target and vehicle controls."""

import json
import math
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import (
    QPointF,
    QProcess,
    QRect,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    QUrlQuery,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from demo_site import SITE, SITE_ZOOM
from display import TargetNames
from map_tracks import GPS_GAP_NS, MapTracks


def tile_url(layer, z, x, y):
    if layer == "Street":
        return QUrl(f"https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    if z <= 16:
        return QUrl(
            f"https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
        )
    # Higher-detail US NAIP aerial imagery, requested on the same Mercator grid.
    half_world = 20037508.342789244
    side = 2 * half_world / 2**z
    west, north = x * side - half_world, half_world - y * side
    url = QUrl(
        "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer/exportImage"
    )
    query = QUrlQuery()
    query.addQueryItem(
        "bbox", ",".join(f"{v:.6f}" for v in (west, north - side, west + side, north))
    )
    for name, value in (
        ("bboxSR", "3857"),
        ("imageSR", "3857"),
        ("size", "256,256"),
        ("format", "jpg"),
        ("f", "image"),
    ):
        query.addQueryItem(name, value)
    url.setQuery(query)
    return url


def obstacle_point(painter, pt):
    painter.setPen(QPen(QColor("#382c1f"), 2))
    painter.setBrush(QColor("#ffc15c"))
    painter.drawPolygon(
        QPolygonF(
            [pt + QPointF(0, -6), pt + QPointF(6, 0), pt + QPointF(0, 6), pt + QPointF(-6, 0)]
        )
    )


class RunwayMap(QWidget):
    pointPicked = Signal(float, float)
    targetPicked = Signal(str)
    panned = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(195)
        self.center = SITE
        self.initial_view = True
        self.robot = self.last_known = None
        self.obstacles = ()
        self.layer = "Satellite"
        self.overview = QImage(str(Path(__file__).resolve().parent / "demo" / "site-overview.jpg"))
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.selected = ""
        self.tracks = MapTracks()
        self.preview_location = self.drag = None
        self.drag_moved = False
        self.zoom = SITE_ZOOM
        self.targets = {}
        self.target_name = TargetNames()
        self.active = ""
        self.attribution = QLabel(self)
        self.attribution.setStyleSheet("background: white; color: #365851; padding: 2px 6px;")
        self.attribution.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.tiles = {}
        self.pending = ()
        self.batch = None
        self.worker = QProcess(self)
        self.worker.finished.connect(self.batch_finished)
        self.worker.errorOccurred.connect(self.worker_error)
        self.worker_guard = QTimer(self)
        self.worker_guard.setSingleShot(True)
        self.worker_guard.timeout.connect(self.worker.kill)
        self.wanted = set()
        self.fetch = QTimer(self)
        self.fetch.setSingleShot(True)
        self.fetch.timeout.connect(self.fetch_visible)
        self.failed = set()
        self.retry = QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.timeout.connect(self.retry_tiles)
        self.update_attribution()

    def retry_tiles(self):
        self.failed.clear()
        self.update_attribution()
        self.update()

    def layer_switch(self):
        switch = QWidget()
        switch.setObjectName("layerSwitch")
        row = QHBoxLayout(switch)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(0)
        group = QButtonGroup(switch)
        for name in ("Satellite", "Street"):
            btn = QPushButton(name)
            btn.setAccessibleName(f"{name} basemap")
            btn.setObjectName("layerButton")
            btn.setCheckable(True)
            btn.setChecked(name == self.layer)
            group.addButton(btn)
            row.addWidget(btn)
        group.buttonClicked.connect(lambda button: self.set_layer(button.text()))
        return switch

    def set_layer(self, name):
        self.layer = name
        self.retry_tiles()

    def preview(self, lat, lon):
        if not (-85.0511 <= lat <= 85.0511 and -180 <= lon <= 180):
            raise ValueError("The map covers latitudes within ±85.05°.")
        self.initial_view = False
        self.preview_location = self.center = lat, lon
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.failed.clear()
        self.update()

    def pixel(self, lat, lon):
        n = 256 * 2**self.zoom
        angle = math.radians(max(-85.0511, min(85.0511, lat)))
        return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(angle)) / math.pi) * n / 2

    def screen_point(self, location):
        cx, cy = self.pixel(*self.center)
        x, y = self.pixel(*location)
        width = 256 * 2**self.zoom
        return QPointF(
            self.width() / 2 + (x - cx + width / 2) % width - width / 2,
            self.height() / 2 + y - cy,
        )

    def fit_targets(self):
        points = [p for p in self.targets.values() if abs(p[0]) <= 85.0511]
        points.extend(self.obstacles)
        points.extend(p for p in self.tracks.ahead if abs(p[0]) <= 85.0511)
        points.extend(p for _, p in self.tracks.fixes if abs(p[0]) <= 85.0511)
        if self.robot or self.last_known:
            points.append((self.robot or self.last_known)[:2])
        if not points:
            return
        self.initial_view = False
        # Cut the longitude circle at its largest gap so targets across 180° stay together.
        lons = sorted(lon % 360 for _, lon in points)
        gaps = [b - a for a, b in zip(lons, lons[1:] + [lons[0] + 360])]
        cut = gaps.index(max(gaps))
        west = lons[(cut + 1) % len(lons)]
        lon = (west + (360 - max(gaps)) / 2 + 180) % 360 - 180
        ys = [math.asinh(math.tan(math.radians(lat))) for lat, _ in points]
        self.center = math.degrees(math.atan(math.sinh((min(ys) + max(ys)) / 2))), lon
        for zoom in range(24, 2, -1):
            self.zoom = zoom
            if all(
                self.rect().adjusted(30, 20, -30, -20).contains(self.screen_point(p).toPoint())
                for p in points
            ):
                break
        self.update()

    def load(self, key):
        if key not in self.pending and key not in self.failed:
            self.wanted.add(key)

    def fetch_visible(self):
        if self.pending or not self.wanted:
            return
        self.pending = tuple(sorted(self.wanted)[:12])
        self.wanted.clear()
        try:
            self.batch = TemporaryDirectory(prefix="ravenstrike-map-")
            urls = [tile_url(*key).toString() for key in self.pending]
            Path(self.batch.name, "urls.json").write_text(json.dumps(urls), encoding="utf-8")
        except OSError:
            self.batch_finished()
            return
        args = (
            ["--fetch-map-tiles"]
            if getattr(sys, "frozen", False)
            else [str(Path(__file__).with_name("map_tiles.py"))]
        )
        self.worker.start(sys.executable, [*args, self.batch.name])
        self.worker_guard.start(30000)

    def stop_tiles(self):
        self.fetch.stop()
        if self.worker.state() != QProcess.ProcessState.NotRunning:
            self.worker.kill()
            self.worker.waitForFinished(1000)
        self.retry.stop()

    def worker_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.batch_finished()

    def batch_finished(self, *args):
        self.worker_guard.stop()
        for index, key in enumerate(self.pending):
            try:
                data = Path(self.batch.name, str(index)).read_bytes() if self.batch else b""
            except OSError:
                data = b""
            if len(data) == 256 * 256 * 4:
                # Workers return plain pixels. The image owns its buffer after this copy.
                image = QImage(data, 256, 256, 1024, QImage.Format.Format_RGB32).copy()
                if len(self.tiles) >= 96:
                    self.tiles.pop(next(iter(self.tiles)))
                self.tiles[key] = image
            else:
                self.failed.add(key)
        if self.batch:
            self.batch.cleanup()
            self.batch = None
        self.pending = ()
        if self.failed and not self.retry.isActive():
            self.retry.start(30000)
        self.update()

    def update_attribution(self):
        source = (
            "USGS · The National Map / USDA NAIP"
            if self.layer == "Satellite"
            else "© OpenStreetMap contributors"
        )
        self.attribution.setText(source)
        self.attribution.adjustSize()
        self.attribution.move(
            max(0, self.width() - self.attribution.width()),
            max(0, self.height() - self.attribution.height()),
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_attribution()

    def reset_view(self):
        self.center, self.zoom = SITE, SITE_ZOOM
        self.initial_view = True
        self.obstacles = ()
        self.preview_location = self.drag = None
        self.drag_moved = False
        self.active = ""
        self.targets.clear()
        self.target_name = TargetNames()
        self.robot = self.last_known = None
        self.tracks = MapTracks()
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()

    def change_zoom(self, steps):
        if self.center:
            self.initial_view = False
            self.zoom = max(3, min(24, self.zoom + steps))
            self.update()

    def wheelEvent(self, event):
        if event.angleDelta().y():
            self.change_zoom(1 if event.angleDelta().y() > 0 else -1)
        event.accept()

    def mousePressEvent(self, event):
        if self.center and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            # Keep plain coordinates. The native mouse event expires after this callback.
            self.drag = (pos.x(), pos.y(), *self.pixel(*self.center))
            self.drag_moved = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag:
            sx, sy, x, y = self.drag
            dx, dy = event.position().x() - sx, event.position().y() - sy
            if not self.drag_moved:
                if abs(dx) + abs(dy) < QApplication.startDragDistance():
                    return
                self.drag_moved = True
                self.initial_view = False
                self.panned.emit()
            size = 256 * 2**self.zoom
            y = max(0, min(1, (y - dy) / size))
            self.center = (
                math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y)))),
                ((x - dx) / size * 360) % 360 - 180,
            )
            self.update()

    def mouseReleaseEvent(self, event):
        if self.drag and event.button() == Qt.MouseButton.LeftButton:
            sx, sy, x, y = self.drag
            self.drag = None
            pos = event.position()
            if (
                not self.drag_moved
                and abs(pos.x() - sx) + abs(pos.y() - sy) < QApplication.startDragDistance()
            ):
                nearest = min(
                    self.targets,
                    key=lambda key: (self.screen_point(self.targets[key]) - pos).manhattanLength(),
                    default=None,
                )
                if (
                    nearest
                    and (self.screen_point(self.targets[nearest]) - pos).manhattanLength() <= 12
                ):
                    self.targetPicked.emit(nearest)
                else:
                    size = 256 * 2**self.zoom
                    fy = max(0, min(1, (y + pos.y() - self.height() / 2) / size))
                    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * fy))))
                    lon = (x + pos.x() - self.width() / 2) / size * 360 % 360 - 180
                    self.pointPicked.emit(lat, lon)
            event.accept()

    def draw_tracks(self, painter):
        def stroke(segment, color, width, style):
            if not segment:
                return
            line = QPolygonF(segment)
            for ink, size, pattern in (
                ("#ffffff", width + 2, Qt.PenStyle.SolidLine),
                (color, width, style),
            ):
                painter.setPen(
                    QPen(
                        QColor(ink),
                        size,
                        pattern,
                        Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin,
                    )
                )
                painter.drawPolyline(line)
            if style == Qt.PenStyle.SolidLine:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(color))
                previous = None
                for pt in segment:
                    if previous is None or (pt - previous).manhattanLength() >= 15:
                        painter.drawEllipse(pt, 2.7, 2.7)
                        previous = pt

        def segments(samples, color, width, style):
            line = []
            last = None
            for stamp, coords in samples:
                valid = abs(coords[0]) <= 85.0511
                pt = self.screen_point(coords)
                if not valid or (
                    line
                    and (
                        stamp - last > GPS_GAP_NS or abs(pt.x() - line[-1].x()) > 128 * 2**self.zoom
                    )
                ):
                    stroke(line, color, width, style)
                    line = []
                if valid:
                    line.append(pt)
                last = stamp
            stroke(line, color, width, style)

        segments(((0, p) for p in self.tracks.ahead), "#3b5cc4", 5, Qt.PenStyle.DashLine)
        segments(self.tracks.fixes, "#007b68", 2.6, Qt.PenStyle.SolidLine)
        painter.setPen(QPen(QColor("#3b5cc4"), 1.6))
        painter.setBrush(Qt.GlobalColor.white)
        for coords in self.tracks.ahead[1:]:
            if abs(coords[0]) <= 85.0511:
                painter.drawEllipse(self.screen_point(coords), 3.3, 3.3)

    def map_label(self, painter, anchor, text):
        box = QRectF(painter.fontMetrics().boundingRect(text)).adjusted(-5, -2, 5, 2)
        box.moveBottomLeft(anchor)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 39, 47, 225))
        painter.drawRoundedRect(box, 3, 3)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def paintEvent(self, event):
        self.wanted.clear()
        with QPainter(self) as painter:
            self.draw_map(painter)
        # Starting a network request can dispatch native events on a desktop.
        # Do that only after Qt has finished painting this widget.
        if self.wanted and not self.pending and not self.fetch.isActive():
            self.fetch.start(0)

    def draw_map(self, p):
        p.fillRect(self.rect(), QColor("#eaf1f0"))
        cx, cy = self.pixel(*self.center)
        left, top = round(cx - self.width() / 2), round(cy - self.height() / 2)
        count = 2**self.zoom
        # Magnify the last available basemap level for metre-scale robot manoeuvres.
        tile_zoom = min(self.zoom, 19)
        tile_count = 2**tile_zoom
        tile_size = 256 * 2 ** (self.zoom - tile_zoom)
        # The bundled NAIP site image is centred on SITE at the level-18 pixel scale.
        # Clip before drawing so metre-scale overzoom never allocates a huge bitmap.
        overview_rect = QRectF()
        if self.layer == "Satellite" and not self.overview.isNull():
            factor = 2.0 ** (self.zoom - 18)
            overview_rect = QRectF(
                0, 0, self.overview.width() * factor, self.overview.height() * factor
            )
            overview_rect.moveCenter(self.screen_point(SITE))
            dst = overview_rect.intersected(QRectF(self.rect()))
            if not dst.isEmpty():
                src = QRectF(
                    (dst.x() - overview_rect.x()) / factor,
                    (dst.y() - overview_rect.y()) / factor,
                    dst.width() / factor,
                    dst.height() / factor,
                )
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.drawImage(dst, self.overview, src)
        missing = False
        for x in range(left // tile_size, (left + self.width()) // tile_size + 1):
            for y in range(
                max(0, top // tile_size), min(tile_count, (top + self.height()) // tile_size + 1)
            ):
                key = self.layer, tile_zoom, x % tile_count, y
                tile_rect = QRect(x * tile_size - left, y * tile_size - top, tile_size, tile_size)
                if self.zoom >= 17 and overview_rect.contains(
                    QRectF(tile_rect.intersected(self.rect()))
                ):
                    # This area already has level-18 imagery.
                    if tile_zoom <= 18:
                        continue
                    if key not in self.tiles:
                        self.load(key)
                        continue
                if key in self.tiles:
                    p.drawImage(
                        QRect(x * tile_size - left, y * tile_size - top, tile_size, tile_size),
                        self.tiles[key],
                    )
                else:
                    missing |= key in self.failed
                    # While overzoomed, keep the visible parent tile until finer detail loads.
                    for levels in range(1, min(tile_zoom, 5) + 1):
                        scale = 2**levels
                        parent = (self.layer, tile_zoom - levels, key[2] // scale, y // scale)
                        if parent in self.tiles:
                            part = 256 // scale
                            p.drawImage(
                                QRect(
                                    x * tile_size - left, y * tile_size - top, tile_size, tile_size
                                ),
                                self.tiles[parent],
                                QRect(key[2] % scale * part, y % scale * part, part, part),
                            )
                            break
                    self.load(key)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.draw_tracks(p)

        for coord in self.obstacles:
            obstacle_point(p, self.screen_point(coord))

        for name, location in self.targets.items():
            pt = self.screen_point(location)
            p.setPen(QPen(Qt.GlobalColor.white, 2))
            p.setBrush(QColor("#bb4c15" if name == self.active else "#186784"))
            p.drawEllipse(pt, 6, 6)
            if name == self.selected:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#09685f"), 2))
                p.drawEllipse(pt, 10, 10)
            self.map_label(p, pt + QPointF(10, -6), self.target_name(name))
        p.setPen(QPen(QColor("#252c35"), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.preview_location:
            p.drawEllipse(self.screen_point(self.preview_location), 9, 9)
        if self.last_known and not self.robot:
            pt = self.screen_point(self.last_known)
            p.setPen(QPen(QColor("#65727c"), 2, Qt.PenStyle.DashLine))
            p.drawEllipse(pt, 8, 8)
            self.map_label(p, pt + QPointF(12, -7), "Last fix")
        if self.robot:
            pt = self.screen_point(self.robot[:2])
            p.save()
            p.translate(pt)
            p.setPen(QPen(Qt.GlobalColor.white, 2))
            p.setBrush(QColor("#253b72"))
            bearing = self.robot[2]
            if bearing is None:
                p.drawEllipse(QPointF(), 7, 7)
            else:
                p.rotate(bearing)
                p.drawPolygon(
                    QPolygonF([QPointF(0, -12), QPointF(8, 8), QPointF(0, 5), QPointF(-8, 8)])
                )
            p.restore()
            p.setPen(QColor("#253b72"))
            self.map_label(p, pt + QPointF(11, -6), "UGV")
        # Ground distance represented by one Web Mercator screen pixel at the center.
        metres_per_px = 40075016.686 * math.cos(math.radians(self.center[0])) / (256 * count)
        ideal = max(1, metres_per_px * 72)
        decade = 10 ** math.floor(math.log10(ideal))
        metres = max(step * decade for step in (1, 2, 5) if step * decade <= ideal)
        pixels = max(1, round(metres / metres_per_px))
        distance = f"{metres / 1000:g} km" if metres >= 1000 else f"{metres:g} m"
        box = QRect(7, self.height() - 36, pixels + 16, 30)
        p.fillRect(box, QColor("#ffffff"))
        p.setPen(QPen(QColor("#365851"), 2))
        p.drawText(box.adjusted(0, 0, 0, -9), Qt.AlignmentFlag.AlignCenter, distance)
        y = box.bottom() - 5
        p.drawLine(15, y, 15 + pixels, y)
        p.drawLine(15, y - 4, 15, y)
        p.drawLine(15 + pixels, y - 4, 15 + pixels, y)
        if missing:
            p.setPen(QColor("#365851"))
            p.setBackground(Qt.GlobalColor.white)
            p.setBackgroundMode(Qt.BGMode.OpaqueMode)
            p.drawText(8, 18, "Some tiles unavailable")
