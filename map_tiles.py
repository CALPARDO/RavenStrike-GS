"""Fetch and decode one batch of basemap tiles outside the operator process."""

import json
import sys
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QEventLoop,
    QLockFile,
    QStandardPaths,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkDiskCache,
    QNetworkReply,
    QNetworkRequest,
    QSslSocket,
)


def main(directory):
    app = QCoreApplication([])
    app.setApplicationName("RavenStrike-GS")
    directory = Path(directory)
    urls = json.loads((directory / "urls.json").read_text(encoding="utf-8"))
    if not 1 <= len(urls) <= 12:
        raise ValueError("Invalid tile batch size")
    if sys.platform == "win32" and "schannel" in QSslSocket.availableBackends():
        QSslSocket.setActiveBackend("schannel")
    network = QNetworkAccessManager()
    cache_path = (
        Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)) / "osm"
    )
    cache_path.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(cache_path / "writer.lock"))
    if lock.tryLock(0):
        cache = QNetworkDiskCache(network)
        cache.setCacheDirectory(str(cache_path))
        cache.setMaximumCacheSize(32 * 1024 * 1024)
        network.setCache(cache)

    replies = []
    loop = QEventLoop()

    def check_finished():
        if all(reply.isFinished() for reply in replies):
            loop.quit()

    for url in urls:
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(
            b"User-Agent", b"RavenStrike-GS/0.1 (runway debris research ground station)"
        )
        request.setAttribute(
            QNetworkRequest.Attribute.CacheLoadControlAttribute,
            QNetworkRequest.CacheLoadControl.PreferCache,
        )
        request.setTransferTimeout(20000)
        reply = network.get(request)
        replies.append(reply)
        reply.finished.connect(check_finished)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(22000)
    if not all(reply.isFinished() for reply in replies):
        loop.exec()
    for index, reply in enumerate(replies):
        if not reply.isFinished():
            reply.abort()
            continue
        if reply.error() != QNetworkReply.NetworkError.NoError:
            continue
        reader = QImageReader(reply)
        size = reader.size()
        if (size.width(), size.height()) != (256, 256):
            continue
        image = reader.read().convertToFormat(QImage.Format.Format_RGB32)
        if not image.isNull():
            (directory / str(index)).write_bytes(image.constBits().tobytes())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
