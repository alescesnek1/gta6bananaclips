"""
OpenCV cannot open a file whose path has non-ASCII characters on Windows (19. 11.: on the 24/7 PC the venv sits
under a folder with a diacritic, every Haar cascade loaded empty and every YouTube render failed with
'CascadeClassifier::detectMultiScale ... !empty()'). cascade() copies the XML to an ASCII folder first when needed.
"""
import os
import shutil
import tempfile
from pathlib import Path

import cv2


def ascii_dir():
    for d in (os.environ.get("KLIPY_OUT"), tempfile.gettempdir(), r"C:\Users\Public"):
        if d and str(d).isascii():
            p = Path(d) / "_cv"
            p.mkdir(parents=True, exist_ok=True)
            return p
    raise RuntimeError("no ASCII folder for the OpenCV cascades")


def cascade(name):
    """A loaded Haar cascade by file name (cv2.data.haarcascades); raises instead of returning an empty one."""
    src = Path(cv2.data.haarcascades) / name
    path = src
    if not str(src).isascii():
        path = ascii_dir() / name
        if not path.exists() or path.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, path)
    c = cv2.CascadeClassifier(str(path))
    if c.empty():
        raise RuntimeError(f"OpenCV cascade {name} did not load from {path}")
    return c


def ascii_path(p):
    """p itself when its path is ASCII, else a copy (file or whole folder) in an ASCII folder. For paths ffmpeg
    reads INSIDE a filter (subtitles fontsdir, drawtext fontfile): libass / fontconfig crashed on the 24/7 PC
    (0xC0000005, 'Fontconfig error: Cannot load default config file') with the repo under a non-ASCII folder."""
    p = Path(p)
    if str(p).isascii() or not p.exists():
        return p
    dst = ascii_dir() / ("_".join(p.parts[-2:]) if p.is_dir() else p.name)
    if p.is_dir():
        shutil.copytree(p, dst, dirs_exist_ok=True)
    elif not dst.exists() or dst.stat().st_size != p.stat().st_size:
        shutil.copyfile(p, dst)
    return dst
