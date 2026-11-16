"""The WordPress plugin, handed out by the portal itself.

Its source is in ``wordpress/qqplay`` of the repository. A school downloads
it from its integration's page as the zip WordPress installs (Plugins › Add
New › Upload Plugin), so the plugin always matches the portal it came from.
"""
from __future__ import annotations

import io
import zipfile
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_GET

FOLDER = "qqplay"
#: One date for every file, so the same source always makes the same zip.
STAMP = (2026, 1, 1, 0, 0, 0)


def source() -> Path:
    return Path(settings.BASE_DIR) / "wordpress" / FOLDER


@lru_cache(maxsize=1)
def package() -> bytes:
    """The plugin's folder as a zip, with the folder as its root."""
    root = source()
    if not (root / "qqplay.php").is_file():
        raise FileNotFoundError(root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            # Translation sources stay in the repository; WordPress reads the .mo.
            if relative.endswith((".pot", ".po")) or any(part.startswith(".") for part in path.parts):
                continue
            info = zipfile.ZipInfo(f"{FOLDER}/{relative}", date_time=STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def version() -> str:
    """The plugin's version, as its header says."""
    try:
        for line in (source() / "qqplay.php").read_text(encoding="utf-8").splitlines()[:30]:
            if "Version:" in line and "Requires" not in line:
                return line.split("Version:", 1)[1].strip()
    except OSError:
        pass
    return ""


@require_GET
def download(request):
    try:
        data = package()
    except FileNotFoundError as error:
        raise Http404("plugin not found") from error
    response = HttpResponse(data, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{FOLDER}.zip"'
    response["Content-Length"] = str(len(data))
    response["Cache-Control"] = "no-cache"
    return response
