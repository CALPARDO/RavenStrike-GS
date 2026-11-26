"""Exercise the built desktop binary with a real, local HTTP tile service."""

import os
import subprocess
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from PySide6.QtGui import QImage

binary = {
    "darwin": "RavenStrike-GS.app/Contents/MacOS/RavenStrike-GS",
    "win32": "RavenStrike-GS/RavenStrike-GS.exe",
}.get(sys.platform, "RavenStrike-GS/RavenStrike-GS")
binary = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path("dist", binary).resolve()
with TemporaryDirectory() as folder:
    tile = QImage(256, 256, QImage.Format.Format_RGB32)
    tile.fill(0xFF336699)
    assert tile.save(str(Path(folder, "tile.png")))
    site = Path(__file__).resolve().parents[1] / "demo/site-overview.jpg"
    assert QImage(str(site)).copy(640, 420, 256, 256).save(str(Path(folder, "tile.jpg")))
    handler = partial(SimpleHTTPRequestHandler, directory=folder)
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            for scale in ("1", "1.5") if sys.platform == "win32" else ("1",):
                env = {**os.environ, "QT_SCALE_FACTOR": scale}
                env["QT_QPA_PLATFORM"] = "windows" if sys.platform == "win32" else "offscreen"
                subprocess.run(
                    [str(binary), "--self-test", f"http://127.0.0.1:{server.server_port}"],
                    check=True,
                    timeout=90,
                    env=env,
                )
        finally:
            server.shutdown()
