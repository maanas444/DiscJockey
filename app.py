"""
DiscJockey - an aesthetic audio CD player for Windows.
Features: drive scanning, album art + track names (MusicBrainz / Cover Art Archive),
seek slider, previous/next, playback speed, shuffle, repeat (off / all / one),
a spinning-vinyl mode and an ambient / fullscreen mode with a blurred cover backdrop,
and a system-tray mode that wakes the window when a disc is inserted.
"""
import base64
import ctypes
import hashlib
import json
import math
import os
import random
import sys
import threading
import time
import urllib.request
from ctypes import wintypes

if getattr(sys, "frozen", False):  # running as the packaged .exe
    _vlc_dir = os.path.join(sys._MEIPASS, "vlc")
    os.environ.setdefault("PYTHON_VLC_LIB_PATH", os.path.join(_vlc_dir, "libvlc.dll"))
    os.environ.setdefault("VLC_PLUGIN_PATH", os.path.join(_vlc_dir, "plugins"))
try:
    import vlc
except Exception:  # pragma: no cover
    vlc = None

from PySide6.QtCore import (QAbstractNativeEventFilter, QObject, QPoint, QPointF, QRectF,
                            QSettings, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QFont, QImage, QKeySequence,
                           QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient, QShortcut)
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QListWidget,
                               QMenu, QMessageBox, QPushButton, QSizePolicy, QSlider,
                               QStackedLayout, QStyle, QStyleOptionSlider, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

USER_AGENT = "DiscJockey/1.2 (personal hobby project)"
QWIDGETSIZE_MAX = 16777215

# --------------------------------------------------------------------------- #
# Disc access (Windows): list drives, read the table of contents
# --------------------------------------------------------------------------- #

def list_cd_drives():
    """Return drive letters (e.g. ['D']) of optical drives."""
    if sys.platform != "win32":
        return []
    k32 = ctypes.windll.kernel32
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT
    mask = k32.GetLogicalDrives()
    drives = []
    for i in range(26):
        if mask & (1 << i):
            letter = chr(ord("A") + i)
            if k32.GetDriveTypeW(f"{letter}:\\") == 5:  # DRIVE_CDROM
                drives.append(letter)
    return drives


def read_toc(letter):
    """
    Read the CD table of contents via IOCTL_CDROM_READ_TOC.
    Returns dict(first, last, offsets{track: frame_offset}, leadout, audio[list of track nums])
    or None if there is no readable disc.
    """
    if sys.platform != "win32":
        return None
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.DeviceIoControl.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    GENERIC_READ, SHARE_RW, OPEN_EXISTING = 0x80000000, 3, 3
    invalid = ctypes.c_void_p(-1).value
    h = k32.CreateFileW(f"\\\\.\\{letter}:", GENERIC_READ, SHARE_RW, None, OPEN_EXISTING, 0, None)
    if h is None or h == invalid:
        return None
    try:
        buf = ctypes.create_string_buffer(804)  # 4 header + 100 * 8
        returned = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(h, 0x00024000, None, 0, buf, 804, ctypes.byref(returned), None)
        if not ok:
            return None
    finally:
        k32.CloseHandle(h)
    data = buf.raw
    length = (data[0] << 8) | data[1]
    first, last = data[2], data[3]
    count = (length - 2) // 8
    offsets, audio, leadout = {}, [], None
    for i in range(count):
        base = 4 + i * 8
        control = data[base + 1] & 0x0F
        number = data[base + 2]
        m, s, f = data[base + 5], data[base + 6], data[base + 7]
        frames = (m * 60 + s) * 75 + f  # includes the 150-frame lead-in
        if number == 0xAA:
            leadout = frames
        elif 1 <= number <= 99:
            offsets[number] = frames
            if not (control & 0x04):  # bit 2 set = data track
                audio.append(number)
    if leadout is None or not audio:
        return None
    return {"first": first, "last": last, "offsets": offsets, "leadout": leadout, "audio": audio}


IOCTL_STORAGE_CHECK_VERIFY = 0x002D4800


def disc_present(letter):
    """True if the drive currently has media. Doesn't read the disc, so it's cheap."""
    if sys.platform != "win32":
        return False
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.DeviceIoControl.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = ctypes.c_void_p(-1).value
    h = k32.CreateFileW(f"\\\\.\\{letter}:", 0x80000000, 3, None, 3, 0, None)
    if h is None or h == invalid:
        return False
    try:
        returned = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(h, IOCTL_STORAGE_CHECK_VERIFY, None, 0, None, 0,
                                 ctypes.byref(returned), None)
        return bool(ok)
    finally:
        k32.CloseHandle(h)


def track_durations(toc):
    """Duration in ms of each audio track."""
    result = {}
    nums = sorted(toc["offsets"])
    for n in toc["audio"]:
        start = toc["offsets"][n]
        nxt = [toc["offsets"][k] for k in nums if k > n]
        end = nxt[0] if nxt else toc["leadout"]
        result[n] = int((end - start) / 75 * 1000)
    return result


def musicbrainz_disc_id(toc):
    s = "%02X%02X" % (toc["first"], toc["last"])
    entries = [toc["leadout"]]
    for t in range(1, 100):
        entries.append(toc["offsets"].get(t, 0) if toc["first"] <= t <= toc["last"] else 0)
    s += "".join("%08X" % e for e in entries)
    digest = base64.b64encode(hashlib.sha1(s.encode("ascii")).digest()).decode("ascii")
    return digest.replace("+", ".").replace("/", "_").replace("=", "-")


# --------------------------------------------------------------------------- #
# Disc arrival detection (Windows): wakes the app when media goes into a drive
# --------------------------------------------------------------------------- #

WM_DEVICECHANGE = 0x0219
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVTYP_VOLUME = 0x00000002


class DEV_BROADCAST_VOLUME(ctypes.Structure):
    _fields_ = [("dbcv_size", wintypes.DWORD),
                ("dbcv_devicetype", wintypes.DWORD),
                ("dbcv_reserved", wintypes.DWORD),
                ("dbcv_unitmask", wintypes.DWORD),
                ("dbcv_flags", wintypes.WORD)]


class DiscArrivalFilter(QAbstractNativeEventFilter):
    """Calls back with the drive letter when Windows reports media in an optical drive."""

    def __init__(self, on_arrival):
        super().__init__()
        self.on_arrival = on_arrival

    def nativeEventFilter(self, event_type, message):
        if sys.platform == "win32" and bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_DEVICECHANGE and msg.wParam == DBT_DEVICEARRIVAL and msg.lParam:
                info = DEV_BROADCAST_VOLUME.from_address(msg.lParam)
                if info.dbcv_devicetype == DBT_DEVTYP_VOLUME:
                    cd_drives = set(list_cd_drives())
                    for i in range(26):
                        letter = chr(ord("A") + i)
                        if info.dbcv_unitmask & (1 << i) and letter in cd_drives:
                            # Defer so the event handler returns quickly
                            QTimer.singleShot(0, lambda l=letter: self.on_arrival(l))
        return False, 0


# --------------------------------------------------------------------------- #
# Metadata + cover art (MusicBrainz / Cover Art Archive)
# --------------------------------------------------------------------------- #

def _get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout).read()


def credit_name(credit):
    return "".join((c.get("name") or c.get("artist", {}).get("name", "")) + c.get("joinphrase", "")
                   for c in credit or [])


def _mb_get_json(url, attempts=3):
    for i in range(attempts):
        try:
            return json.loads(_get(url))
        except Exception as e:
            code = getattr(e, "code", None)
            if code == 503 and i < attempts - 1:   # temporary overload: back off and retry
                time.sleep(2 * (i + 1))
                continue
            print("MusicBrainz request failed:", url, e)
            return None
    return None


def fetch_metadata(toc, n_tracks):
    disc_id = musicbrainz_disc_id(toc)
    toc_str = "+".join(str(x) for x in [toc["first"], toc["last"], toc["leadout"]] +
                       [toc["offsets"][t] for t in range(toc["first"], toc["last"] + 1)
                        if t in toc["offsets"]])
    base = "https://musicbrainz.org/ws/2/discid/"
    inc = "inc=recordings+artist-credits&fmt=json"   # 'discids' is not valid on this endpoint
    urls = [f"{base}{disc_id}?{inc}", f"{base}-?toc={toc_str}&{inc}"]
    for url in urls:
        data = _mb_get_json(url)
        if not data:
            continue
        releases = data.get("releases") or []
        print(f"MusicBrainz: {len(releases)} release(s) for this TOC")
        # Every release returned here already matches this TOC, so pick the medium
        # whose track count fits the disc
        for rel in releases:
            for medium in rel.get("media", []):
                if len(medium.get("tracks", [])) != n_tracks:
                    continue
                print("MusicBrainz match:", rel.get("title"), "|", rel.get("id"))
                titles = [t.get("title", "") for t in medium.get("tracks", [])]
                art = None
                for r in [rel] + [x for x in releases[:5] if x is not rel]:
                    try:
                        art = _get(f"https://coverartarchive.org/release/{r['id']}/front-500",
                                   timeout=15)
                        break
                    except Exception:
                        continue
                return {"album": rel.get("title", ""), "artist": credit_name(rel.get("artist-credit")),
                        "titles": titles, "art": art}
    print("MusicBrainz: no matching release found")
    return None


# --------------------------------------------------------------------------- #
# Drawing helpers
# --------------------------------------------------------------------------- #

def rounded(pixmap, size, radius=None):
    radius = radius if radius is not None else max(14, int(size * 0.075))
    pm = pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                       Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.setClipPath(path)
    p.drawPixmap((size - pm.width()) // 2, (size - pm.height()) // 2, pm)
    p.end()
    return out


def default_raw(size=512):
    """Placeholder cover (square, not rounded)."""
    pm = QPixmap(size, size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0, QColor("#3a2c6b"))
    g.setColorAt(1, QColor("#0f2a4a"))
    p.fillRect(0, 0, size, size, QBrush(g))
    c = QPointF(size / 2, size / 2)
    p.setPen(QPen(QColor(255, 255, 255, 40), 3))
    p.setBrush(Qt.BrushStyle.NoBrush)
    for r in (size * 0.40, size * 0.30, size * 0.20):
        p.drawEllipse(c, r, r)
    p.setBrush(QColor(255, 255, 255, 70))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(c, size * 0.06, size * 0.06)
    p.end()
    return pm


def fmt_time(ms):
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"


class Palette:
    """Colours for the whole window, derived from the cover."""

    def __init__(self, bg_top, bg_bottom, accent, glow):
        self.bg_top = QColor(bg_top)
        self.bg_bottom = QColor(bg_bottom)
        self.accent = QColor(accent)
        self.glow = QColor(glow)


DEFAULT_PALETTE = Palette("#3d3070", "#0e0b1f", "#b9a6ff", "#8f7bff")


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def palette_from_image(img):
    """
    Pick a background gradient from the cover's dominant colour and an accent from its
    most vivid colour. Dark / colourless covers get a dark neutral gradient with a bright
    accent (or near-white if the cover has no colour at all), so controls always pop.
    """
    n = 48
    small = img.scaled(n, n, Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation
                       ).convertToFormat(QImage.Format.Format_RGB32)
    bins = 12
    area = [0.0] * bins       # saturation * value, summed: how much "colour" each hue has
    count = [0] * bins
    vivid = [0] * bins        # pixels that are both saturated and bright
    hue_sum = [0.0] * bins
    sat_sum = [0.0] * bins
    lum = 0.0
    for y in range(n):
        for x in range(n):
            c = small.pixelColor(x, y)
            lum += 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
            h, s, v = c.hueF(), c.saturationF(), c.valueF()
            if h < 0 or s < 0.18 or v < 0.15:
                continue                       # ignore greys, near-blacks
            b = min(bins - 1, int(h * bins))
            area[b] += s * v
            count[b] += 1
            hue_sum[b] += h
            sat_sum[b] += s
            if s > 0.35 and v > 0.4:
                vivid[b] += 1
    lum /= n * n
    colourful = sum(area) / (n * n)
    dom = max(range(bins), key=lambda b: area[b])
    have_dom = colourful >= 0.05 and count[dom] > 0
    vb = max(range(bins), key=lambda b: vivid[b])
    have_vivid = vivid[vb] >= 8 and count[vb] > 0

    def mean(b):
        return hue_sum[b] / count[b], sat_sum[b] / count[b]

    if have_dom:
        hd, sd = mean(dom)
        s_bg = _clamp(sd, 0.35, 0.85)
        bg_top = QColor.fromHsvF(hd, s_bg, 0.52 if lum > 0.3 else 0.42)
        bg_bottom = QColor.fromHsvF(hd, min(0.9, s_bg), 0.12)
    else:
        hd = mean(vb)[0] if have_vivid else 0.62
        bg_top = QColor.fromHsvF(hd, 0.16, 0.26)
        bg_bottom = QColor.fromHsvF(hd, 0.22, 0.08)
    if have_vivid:
        ha, sa = mean(vb)
        accent = QColor.fromHsvF(ha, _clamp(sa, 0.45, 0.85), 1.0)
    elif have_dom:
        ha = hd
        accent = QColor.fromHsvF(hd, _clamp(sd, 0.35, 0.7), 1.0)
    else:
        ha = hd
        accent = QColor.fromHsvF(hd, 0.08, 1.0)    # near-white pops on the dark neutral gradient
    colourless = not have_dom and not have_vivid
    glow = QColor.fromHsvF(ha, 0.25 if colourless else 0.8, 0.9 if colourless else 0.95)
    return Palette(bg_top, bg_bottom, accent, glow)


class ArtWidget(QWidget):
    """Flat album art with a soft shadow, scaled to whatever room it gets."""

    def __init__(self):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.raw = default_raw()
        self._cache = None

    def set_pixmap(self, raw):
        self.raw = raw
        self._cache = None
        self.update()

    def paintEvent(self, _):
        side = min(self.width(), self.height())
        inner = int(side * 0.92)
        if inner < 20:
            return
        if self._cache is None or self._cache[0] != inner:
            self._cache = (inner, rounded(self.raw, inner))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = (self.width() - inner) / 2
        y = (self.height() - inner) / 2
        radius = max(14, int(inner * 0.075))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 14))
        for i in range(8):
            grow = i * 1.0
            p.drawRoundedRect(QRectF(x - grow, y - grow + 5, inner + 2 * grow, inner + 2 * grow),
                              radius + grow, radius + grow)
        p.drawPixmap(int(x), int(y), self._cache[1])


class VinylWidget(QWidget):
    """A turntable: spinning record with the cover as its label, plus a tonearm."""
    DEG_PER_SEC = 200.0                      # 33 1/3 rpm
    REST_PHI, PLAY_PHI, DRIFT = 84.0, 102.0, 15.0
    PIVOT = (0.88, 0.14)
    CENTER = (0.47, 0.55)
    RADIUS = 0.43
    ARM_LEN = 0.58

    def __init__(self):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.label = None
        self.glow = QColor("#8f7bff")
        self.angle = 0.0
        self.speed = 0.0
        self.arm_phi = self.REST_PHI
        self.playing = False
        self.rate = 1.0
        self.progress = 0.0
        self._disc = None
        self._last = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)

    # -- state from the player -------------------------------------------------
    def set_label(self, raw):
        self.label = raw
        self._disc = None
        self.update()

    def set_glow(self, color):
        self.glow = QColor(color)
        self.update()

    def set_state(self, playing, rate):
        self.playing = playing
        self.rate = rate

    def set_progress(self, frac):
        self.progress = max(0.0, min(1.0, frac))

    # -- lifecycle -------------------------------------------------------------
    def showEvent(self, e):
        self._last = time.monotonic()
        self.timer.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def resizeEvent(self, e):
        self._disc = None
        super().resizeEvent(e)

    def _tick(self):
        now = time.monotonic()
        dt = min(0.05, now - self._last)
        self._last = now
        target_speed = self.DEG_PER_SEC * self.rate if self.playing else 0.0
        target_phi = (self.PLAY_PHI + self.DRIFT * self.progress) if self.playing else self.REST_PHI
        changed = False
        if abs(self.speed - target_speed) > 0.05:
            self.speed += (target_speed - self.speed) * (1 - math.exp(-dt * 1.6))
            changed = True
        else:
            self.speed = target_speed
        if abs(self.arm_phi - target_phi) > 0.02:
            self.arm_phi += (target_phi - self.arm_phi) * (1 - math.exp(-dt * 2.4))
            changed = True
        else:
            self.arm_phi = target_phi
        if self.speed:
            self.angle = (self.angle + self.speed * dt) % 360
            changed = True
        if changed:      # idle = no repaint, so it costs nothing sitting in the background
            self.update()

    # -- drawing ---------------------------------------------------------------
    def _build_disc(self, side):
        R = side * self.RADIUS
        dpr = self.devicePixelRatioF()
        D = int(math.ceil(2 * R)) + 4
        pm = QPixmap(int(D * dpr), int(D * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.translate(D / 2, D / 2)
        origin = QPointF(0, 0)
        body = QRadialGradient(origin, R)
        body.setColorAt(0.0, QColor(34, 34, 38))
        body.setColorAt(0.6, QColor(15, 15, 17))
        body.setColorAt(1.0, QColor(28, 28, 32))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(body)
        p.drawEllipse(origin, R, R)
        # grooves
        p.setBrush(Qt.BrushStyle.NoBrush)
        r, i, step = R * 0.40, 0, max(1.8, R * 0.011)
        while r < R * 0.97:
            p.setPen(QPen(QColor(255, 255, 255, 22 if i % 4 == 0 else 9), 1))
            p.drawEllipse(origin, r, r)
            r += step
            i += 1
        # a few darker "gaps" between songs
        p.setPen(QPen(QColor(0, 0, 0, 150), max(2.0, R * 0.012)))
        for frac in (0.52, 0.63, 0.74, 0.85):
            p.drawEllipse(origin, R * frac, R * frac)
        # rim
        p.setPen(QPen(QColor(255, 255, 255, 45), 1.4))
        p.drawEllipse(origin, R - 0.8, R - 0.8)
        # label (cover art)
        lr = R * 0.36
        clip = QPainterPath()
        clip.addEllipse(origin, lr, lr)
        p.save()
        p.setClipPath(clip)
        if self.label is not None and not self.label.isNull():
            n = max(2, int(2 * lr * dpr))
            lab = self.label.scaled(n, n, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
            sw = min(lab.width(), lab.height())
            p.drawPixmap(QRectF(-lr, -lr, 2 * lr, 2 * lr), lab,
                         QRectF((lab.width() - sw) / 2, (lab.height() - sw) / 2, sw, sw))
        else:
            p.fillRect(QRectF(-lr, -lr, 2 * lr, 2 * lr), QColor(70, 55, 120))
        shade = QRadialGradient(origin, lr)
        shade.setColorAt(0.55, QColor(0, 0, 0, 0))
        shade.setColorAt(1.0, QColor(0, 0, 0, 110))
        p.fillRect(QRectF(-lr, -lr, 2 * lr, 2 * lr), shade)
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 150), 2))
        p.drawEllipse(origin, lr, lr)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawEllipse(origin, lr * 0.97, lr * 0.97)
        p.setPen(QPen(QColor(0, 0, 0, 120), 2))
        p.drawEllipse(origin, lr * 1.1, lr * 1.1)
        # spindle hole
        hole = R * 0.03
        p.setPen(QPen(QColor(205, 205, 212), 1.5))
        p.setBrush(QColor(12, 12, 14))
        p.drawEllipse(origin, hole, hole)
        p.end()
        return pm, D

    def _draw_arm(self, p, side, ox, oy, down):
        px, py = ox + side * self.PIVOT[0], oy + side * self.PIVOT[1]
        L = side * self.ARM_LEN
        off = 3 + 9 * (1 - down)             # shadow drifts away when the arm lifts
        none = Qt.PenStyle.NoPen
        p.save()
        p.translate(px + off, py + off * 1.5)
        p.rotate(self.arm_phi)
        sp = QPen(QColor(0, 0, 0, 70), side * 0.018)
        sp.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(sp)
        p.drawLine(QPointF(-side * 0.07, 0), QPointF(L + side * 0.05, 0))
        p.restore()
        p.save()
        p.translate(px, py)
        p.rotate(self.arm_phi)
        p.setPen(none)
        cw = QLinearGradient(0, -side * 0.03, 0, side * 0.03)
        cw.setColorAt(0, QColor(96, 96, 104))
        cw.setColorAt(1, QColor(38, 38, 44))
        p.setBrush(cw)
        p.drawRoundedRect(QRectF(-side * 0.15, -side * 0.03, side * 0.075, side * 0.06),
                          side * 0.012, side * 0.012)
        tube = QLinearGradient(0, -side * 0.008, 0, side * 0.008)
        tube.setColorAt(0, QColor(238, 238, 245))
        tube.setColorAt(1, QColor(135, 135, 148))
        p.setBrush(tube)
        p.drawRoundedRect(QRectF(-side * 0.08, -side * 0.0075, L + side * 0.08, side * 0.015),
                          side * 0.0075, side * 0.0075)
        p.translate(L, 0)
        p.rotate(12)
        hs = QLinearGradient(0, -side * 0.02, 0, side * 0.02)
        hs.setColorAt(0, QColor(72, 72, 80))
        hs.setColorAt(1, QColor(22, 22, 26))
        p.setBrush(hs)
        p.drawRoundedRect(QRectF(-side * 0.012, -side * 0.02, side * 0.075, side * 0.04),
                          side * 0.01, side * 0.01)
        p.setBrush(self.glow)
        p.drawRect(QRectF(side * 0.035, -side * 0.0045, side * 0.022, side * 0.009))
        p.restore()
        base = QRadialGradient(QPointF(px - side * 0.012, py - side * 0.012), side * 0.065)
        base.setColorAt(0, QColor(232, 232, 240))
        base.setColorAt(1, QColor(88, 88, 98))
        p.setPen(QPen(QColor(0, 0, 0, 90), 1))
        p.setBrush(base)
        p.drawEllipse(QPointF(px, py), side * 0.05, side * 0.05)
        p.setPen(none)
        p.setBrush(QColor(30, 30, 34))
        p.drawEllipse(QPointF(px, py), side * 0.02, side * 0.02)

    def paintEvent(self, _):
        w, h = self.width(), self.height()
        side = min(w, h)
        if side < 60:
            return
        ox, oy = (w - side) / 2, (h - side) / 2
        cx, cy = ox + side * self.CENTER[0], oy + side * self.CENTER[1]
        R = side * self.RADIUS
        centre = QPointF(cx, cy)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setPen(Qt.PenStyle.NoPen)
        # soft drop shadow: fades to nothing before it can reach the widget edge
        # (the coloured glow lives in the window background, so it is never clipped)
        sr = min(R * 1.08, h - cy - R * 0.03)
        sh = QRadialGradient(QPointF(cx, cy + R * 0.03), sr)
        for i in range(21):
            t = i / 20
            u = max(0.0, min(1.0, (t - 0.78) / 0.22))
            sh.setColorAt(t, QColor(0, 0, 0, int(165 * (1 - u * u * (3 - 2 * u)))))
        p.setBrush(sh)
        p.drawEllipse(QPointF(cx, cy + R * 0.03), sr, sr)
        # the record itself (cached, just rotated each frame)
        if self._disc is None or self._disc[0] != side:
            pm, D = self._build_disc(side)
            self._disc = (side, pm, D)
        _, pm, D = self._disc
        p.save()
        p.translate(cx, cy)
        p.rotate(self.angle)
        p.drawPixmap(QPointF(-D / 2, -D / 2), pm)
        p.restore()
        # fixed light reflection across the grooves
        ring = QPainterPath()
        ring.addEllipse(centre, R, R)
        hole = QPainterPath()
        hole.addEllipse(centre, R * 0.36, R * 0.36)
        ring = ring.subtracted(hole)
        cg = QConicalGradient(centre, 40)

        def white(a):
            return QColor(255, 255, 255, a)

        for pos, a in ((0.0, 0), (0.05, 62), (0.11, 0), (0.5, 0), (0.55, 48), (0.61, 0), (1.0, 0)):
            cg.setColorAt(pos, white(a))
        p.setBrush(QBrush(cg))
        p.drawPath(ring)
        span = self.REST_PHI - self.PLAY_PHI
        down = max(0.0, min(1.0, (self.REST_PHI - self.arm_phi) / span))
        self._draw_arm(p, side, ox, oy, down)


class ClickSlider(QSlider):
    """Slider that jumps to wherever you click and supports dragging."""

    def _value_at(self, x):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                             QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                             QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())
        pos = int(x - handle.width() / 2 - groove.x())
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), pos, span)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._value_at(e.position().x()))
            self.setSliderDown(True)
            e.accept()

    def mouseMoveEvent(self, e):
        if self.isSliderDown():
            self.setValue(self._value_at(e.position().x()))
            e.accept()

    def mouseReleaseEvent(self, e):
        if self.isSliderDown():
            self.setSliderDown(False)
            e.accept()


class Bridge(QObject):
    meta = Signal(object)


class DiscMonitor(QObject):
    """Background thread that watches the selected drive, so the UI never waits on the disc."""
    disc_changed = Signal(str, bool, object)   # drive letter, disc present?, toc or None
    MAX_RETRIES = 8                            # disc may still be spinning up

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._letter = None
        self._present = None           # last presence state we reported
        self._retries = 0
        self._retry_pending = False    # True when the last read failed and should be retried
        threading.Thread(target=self._run, daemon=True).start()

    def set_drive(self, letter):
        with self._lock:
            self._letter = letter
            self._present = None
            self._retries = 0
            self._retry_pending = False
        self._wake.set()

    def poke(self):
        """Check right away (called when Windows reports a disc arrival)."""
        self._wake.set()

    def _run(self):
        while True:
            with self._lock:
                letter = self._letter
            if letter:
                self._check(letter)
            self._wake.wait(1.5)
            self._wake.clear()

    def _check(self, letter):
        present = disc_present(letter)
        with self._lock:
            if letter != self._letter:
                return
            if present != self._present:
                self._present = present
                self._retries = 0
                self._retry_pending = False
            elif not self._retry_pending:
                return                          # nothing has changed
            self._retry_pending = False
        if not present:
            self.disc_changed.emit(letter, False, None)
            return
        toc = read_toc(letter)
        with self._lock:
            if letter != self._letter:
                return                          # user switched drives mid-read
            if toc is None and self._retries < self.MAX_RETRIES:
                self._retries += 1
                self._retry_pending = True      # disc may still be spinning up: try again
                return
        self.disc_changed.emit(letter, True, toc)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class Player(QWidget):
    NORMAL_SIZE = QSize(430, 860)
    AMBIENT_SIZE = QSize(560, 820)
    STAGE_H = 340

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DiscJockey")
        self.resize(self.NORMAL_SIZE)
        self.setWindowState(Qt.WindowState.WindowMaximized)  # open maximised by default
        self.settings = QSettings("DiscJockey", "DiscJockey")
        self.vlc = vlc.Instance("--no-video", "--quiet") if vlc else None
        self.mp = self.vlc.media_player_new() if self.vlc else None
        self.drive = None
        self.toc = None
        self.tracks = []          # [{num, dur, title}]
        self.album = ""
        self.artist = ""
        self.order, self.pos = [], 0
        self.shuffle = False
        self.repeat = 0           # 0 off, 1 all, 2 one
        self.rate = 1.0
        self.playing = False
        self.seeking = False
        self.started_at = 0.0
        self.colors = DEFAULT_PALETTE
        self.backdrop = None
        self.vinyl_mode = self.settings.value("vinyl", True, type=bool)
        self.ambient = False
        self.quitting = False
        self.current_key = None        # MusicBrainz disc ID of the disc on screen
        self.first_check_done = False  # so a disc already in the drive doesn't pop the window
        self.bridge = Bridge()
        self.bridge.meta.connect(self.apply_meta)
        self.build_ui()
        self.apply_theme(self.colors)
        self.set_vinyl(self.vinyl_mode)
        self.set_ambient(self.settings.value("ambient", False, type=bool))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(200)
        QShortcut(QKeySequence("Space"), self, self.toggle_play)
        QShortcut(QKeySequence("Ctrl+Right"), self, lambda: self.next_track())
        QShortcut(QKeySequence("Ctrl+Left"), self, self.prev_track)
        QShortcut(QKeySequence("V"), self, lambda: self.set_vinyl(not self.vinyl_mode))
        QShortcut(QKeySequence("A"), self, lambda: self.set_ambient(not self.ambient))
        QShortcut(QKeySequence("F11"), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("Esc"), self, self.on_escape)
        if not self.vlc:
            QMessageBox.critical(self, "VLC not found",
                                 "Install VLC (64-bit) and run:  pip install python-vlc")

        # system tray: DiscJockey keeps running here when the window is closed
        self.tray_menu = QMenu()
        self.tray_menu.addAction("Open DiscJockey", self.bring_to_front)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction("Quit", self.quit_app)
        self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay), self)
        self.tray.setToolTip("DiscJockey")
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(
            lambda reason: self.bring_to_front()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

        # background disc monitor: spots ejects and inserts without freezing the UI
        self.monitor = DiscMonitor()
        self.monitor.disc_changed.connect(self.on_disc_changed)
        self.scan_drives()

    # ---- UI ---------------------------------------------------------------- #

    def build_ui(self):
        root = QVBoxLayout(self)
        self.root = root
        root.setContentsMargins(28, 16, 28, 22)
        root.setSpacing(10)
        # mode row
        modes = QHBoxLayout()
        brand = QLabel("DISCJOCKEY")
        brand.setObjectName("brand")
        f = brand.font()
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        brand.setFont(f)
        modes.addWidget(brand)
        modes.addStretch(1)
        self.b_vinyl = self.icon_button("◉", checkable=True, tip="Vinyl mode (V)", size=38)
        self.b_ambient = self.icon_button("⛶", checkable=True, tip="Ambient mode (A) · F11 for fullscreen",
                                          size=38)
        self.b_pin = self.icon_button("📌", checkable=True, tip="Keep window on top", size=38)
        self.b_vinyl.clicked.connect(lambda: self.set_vinyl(self.b_vinyl.isChecked()))
        self.b_ambient.clicked.connect(lambda: self.set_ambient(self.b_ambient.isChecked()))
        self.b_pin.clicked.connect(self.toggle_pin)
        for b in (self.b_vinyl, self.b_ambient, self.b_pin):
            modes.addWidget(b)
        root.addLayout(modes)
        # drive row
        self.top_bar = QWidget()
        top = QHBoxLayout(self.top_bar)
        top.setContentsMargins(0, 0, 0, 0)
        self.drive_box = QComboBox()
        self.drive_box.currentIndexChanged.connect(self.on_drive_changed)
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.setObjectName("pill")
        self.scan_btn.clicked.connect(self.scan_drives)
        top.addWidget(self.drive_box, 1)
        top.addWidget(self.scan_btn)
        root.addWidget(self.top_bar)
        # stage: flat art or turntable
        self.stage = QWidget()
        self.stack = QStackedLayout(self.stage)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.art = ArtWidget()
        self.vinyl = VinylWidget()
        self.stack.addWidget(self.art)
        self.stack.addWidget(self.vinyl)
        root.addWidget(self.stage)
        self.title = QLabel("No disc")
        self.title.setObjectName("title")
        self.title.setWordWrap(True)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle = QLabel("Insert an audio CD and press Scan")
        self.subtitle.setObjectName("subtitle")
        self.subtitle.setWordWrap(True)
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.title)
        root.addWidget(self.subtitle)
        self.seek = ClickSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 1)
        self.seek.sliderPressed.connect(lambda: setattr(self, "seeking", True))
        self.seek.sliderReleased.connect(self.on_seek_release)
        self.seek.valueChanged.connect(self.on_seek_value)
        root.addWidget(self.seek)
        times = QHBoxLayout()
        self.t_cur = QLabel("0:00")
        self.t_tot = QLabel("0:00")
        self.t_tot.setAlignment(Qt.AlignmentFlag.AlignRight)
        times.addWidget(self.t_cur)
        times.addWidget(self.t_tot)
        root.addLayout(times)
        ctrl = QHBoxLayout()
        ctrl.setSpacing(14)
        self.b_shuffle = self.icon_button("⤮", checkable=True, tip="Shuffle")
        self.b_prev = self.icon_button("⏮", tip="Previous (Ctrl+Left)")
        self.b_play = self.icon_button("▶", big=True, tip="Play / Pause (Space)")
        self.b_next = self.icon_button("⏭", tip="Next (Ctrl+Right)")
        self.b_repeat = self.icon_button("⟳", checkable=True, tip="Repeat: off")
        self.b_shuffle.clicked.connect(self.toggle_shuffle)
        self.b_prev.clicked.connect(self.prev_track)
        self.b_play.clicked.connect(self.toggle_play)
        self.b_next.clicked.connect(lambda: self.next_track())
        self.b_repeat.clicked.connect(self.cycle_repeat)
        ctrl.addStretch()
        for b in (self.b_shuffle, self.b_prev, self.b_play, self.b_next, self.b_repeat):
            ctrl.addWidget(b)
        ctrl.addStretch()
        root.addLayout(ctrl)
        self.speed_bar = QWidget()
        speed = QHBoxLayout(self.speed_bar)
        speed.setContentsMargins(0, 0, 0, 0)
        speed.addWidget(QLabel("Speed"))
        self.speed = ClickSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(50, 200)  # percent
        self.speed.setValue(100)
        self.speed.valueChanged.connect(self.on_speed)
        self.speed_lbl = QLabel("1.00×")
        self.speed_lbl.setMinimumWidth(46)
        reset = QPushButton("Reset")
        reset.setObjectName("pill")
        reset.clicked.connect(lambda: self.speed.setValue(100))
        speed.addWidget(self.speed, 1)
        speed.addWidget(self.speed_lbl)
        speed.addWidget(reset)
        root.addWidget(self.speed_bar)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self.on_list_activate)
        root.addWidget(self.list, 1)

    def icon_button(self, text, big=False, checkable=False, tip="", size=None):
        b = QPushButton(text)
        b.setObjectName("big" if big else "icon")
        b.setCheckable(checkable)
        b.setToolTip(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        size = size or (64 if big else 44)
        b.setFixedSize(size, size)
        return b

    def apply_theme(self, colors):
        self.colors = colors
        accent = colors.accent.name()
        self.vinyl.set_glow(colors.accent)
        self.setStyleSheet(f"""
        QWidget {{ color: #f2f2f7; font-family: 'Segoe UI'; font-size: 10pt; }}
        QLabel {{ background: transparent; color: #c9c9d6; }}
        QLabel#brand {{ font-size: 8pt; font-weight: 600; color: rgba(255,255,255,0.55); }}
        QLabel#title {{ font-size: 18pt; font-weight: 600; color: white; }}
        QLabel#subtitle {{ font-size: 10.5pt; color: #b8b8c8; }}
        QComboBox {{ background: rgba(255,255,255,0.10); border: none; border-radius: 12px;
                     padding: 8px 12px; }}
        QComboBox QAbstractItemView {{ background: #20202c; selection-background-color: {accent};
                                       selection-color: black; border: none; }}
        QPushButton#pill {{ background: rgba(255,255,255,0.12); border: none; border-radius: 12px;
                            padding: 8px 14px; }}
        QPushButton#pill:hover {{ background: rgba(255,255,255,0.22); }}
        QPushButton#icon {{ background: transparent; border: none; border-radius: 22px;
                            font-family: 'Segoe UI Symbol'; font-size: 17pt; color: #e8e8f0; }}
        QPushButton#icon:hover {{ background: rgba(255,255,255,0.12); }}
        QPushButton#icon:checked {{ color: {accent}; background: rgba(255,255,255,0.08); }}
        QPushButton#big {{ background: {accent}; border: none; border-radius: 32px; color: #111;
                           font-family: 'Segoe UI Symbol'; font-size: 20pt; }}
        QPushButton#big:hover {{ background: white; }}
        QSlider::groove:horizontal {{ height: 6px; background: rgba(255,255,255,0.18);
                                      border-radius: 3px; }}
        QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 3px; }}
        QSlider::handle:horizontal {{ background: white; width: 14px; height: 14px;
                                      margin: -4px 0; border-radius: 7px; }}
        QListWidget {{ background: rgba(0,0,0,0.25); border: none; border-radius: 14px;
                       padding: 6px; outline: none; }}
        QListWidget::item {{ padding: 8px 10px; border-radius: 8px; color: #d6d6e2; }}
        QListWidget::item:hover {{ background: rgba(255,255,255,0.08); }}
        QListWidget::item:selected {{ background: rgba(255,255,255,0.16); color: {accent}; }}
        QComboBox::drop-down {{ border: none; width: 24px; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px 0; }}
        QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.25); border-radius: 4px;
                                       min-height: 24px; }}
        QScrollBar::handle:vertical:hover {{ background: rgba(255,255,255,0.4); }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = self.rect()
        g = QLinearGradient(0, 0, 0, r.height())
        g.setColorAt(0, self.colors.bg_top)
        g.setColorAt(1, self.colors.bg_bottom)
        p.fillRect(r, g)
        if self.backdrop is not None:
            bp = self.backdrop
            scale = max(r.width() / bp.width(), r.height() / bp.height())
            bw, bh = bp.width() * scale, bp.height() * scale
            p.setOpacity(0.55 if self.ambient else 0.35)
            p.drawPixmap(QRectF((r.width() - bw) / 2, (r.height() - bh) / 2, bw, bh), bp,
                         QRectF(bp.rect()))
            p.setOpacity(1.0)
            veil = QLinearGradient(0, 0, 0, r.height())
            veil.setColorAt(0, QColor(0, 0, 0, 60))
            veil.setColorAt(1, QColor(0, 0, 0, 150))
            p.fillRect(r, veil)
        # colour glow behind the record / cover. Painted on the window itself (not inside the
        # record widget) so it fades out smoothly instead of being cut off by a widget edge.
        cx, cy, R = self.record_geometry()
        if R > 10:
            radius = R * 2.3
            glow = QRadialGradient(QPointF(cx, cy), radius)
            for i in range(25):
                t = i / 24
                c = QColor(self.colors.glow)
                c.setAlphaF(0.5 * (1 - t ** 1.5) ** 2)
                glow.setColorAt(t, c)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(QPointF(cx, cy), radius, radius)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

    def record_geometry(self):
        """Centre and radius (in window coordinates) of the record, or of the flat cover."""
        top_left = self.stage.mapTo(self, QPoint(0, 0))
        sw, sh = self.stage.width(), self.stage.height()
        side = min(sw, sh)
        ox, oy = top_left.x() + (sw - side) / 2, top_left.y() + (sh - side) / 2
        if self.vinyl_mode:
            return (ox + side * VinylWidget.CENTER[0], oy + side * VinylWidget.CENTER[1],
                    side * VinylWidget.RADIUS)
        return ox + side / 2, oy + side / 2, side * 0.47

    # ---- View modes -------------------------------------------------------- #

    def set_vinyl(self, on):
        self.vinyl_mode = on
        self.b_vinyl.setChecked(on)
        self.stack.setCurrentWidget(self.vinyl if on else self.art)
        self.settings.setValue("vinyl", on)
        self.update()

    def set_ambient(self, on):
        self.ambient = on
        self.b_ambient.setChecked(on)
        for w in (self.top_bar, self.speed_bar, self.list):
            w.setVisible(not on)
        if on:
            self.stage.setMinimumHeight(220)
            self.stage.setMaximumHeight(QWIDGETSIZE_MAX)
            self.root.setStretchFactor(self.stage, 1)
        else:
            self.stage.setFixedHeight(self.STAGE_H)
            self.root.setStretchFactor(self.stage, 0)
        # Maximise when entering ambient. Leaving ambient keeps the current size.
        if on and not self.isFullScreen():
            self.setWindowState(Qt.WindowState.WindowMaximized)
        self.settings.setValue("ambient", on)
        self.update()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.setWindowState(Qt.WindowState.WindowMaximized)   # back to maximised, not 430×860
        else:
            if not self.ambient:
                self.set_ambient(True)
            self.showFullScreen()

    def on_escape(self):
        if self.isFullScreen():
            self.setWindowState(Qt.WindowState.WindowMaximized)
        elif self.ambient:
            self.set_ambient(False)

    def toggle_pin(self):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.b_pin.isChecked())
        self.show()

    def bring_to_front(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self.quitting = True
        self.stop()
        QApplication.quit()

    def closeEvent(self, e):
        if self.quitting:
            self.stop()
            super().closeEvent(e)
        else:
            e.ignore()
            self.hide()   # keep running in the tray; music keeps playing

    # ---- Cover / backdrop -------------------------------------------------- #

    def set_cover(self, raw):
        self.art.set_pixmap(raw)
        self.vinyl.set_label(raw)

    def set_backdrop(self, img):
        if img is None:
            self.backdrop = None
        else:
            small = img.scaled(20, 20, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
            self.backdrop = QPixmap.fromImage(
                small.scaled(160, 160, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation))
        self.update()

    # ---- Disc loading ------------------------------------------------------ #

    def scan_drives(self):
        self.drive_box.blockSignals(True)
        self.drive_box.clear()
        for d in list_cd_drives():
            self.drive_box.addItem(f"Drive {d}:", d)
        self.drive_box.blockSignals(False)
        if self.drive_box.count() == 0:
            self.stop()
            self.clear_disc("No disc drive found", "Connect a CD drive and press Scan")
            return
        self.on_drive_changed()

    def on_disc_inserted(self, letter):
        # Called by the Windows arrival message. Just wakes the monitor.
        self.monitor.poke()

    def on_drive_changed(self):
        letter = self.drive_box.currentData()
        if not letter:
            return
        self.stop()
        self.drive = letter
        self.toc = None
        self.clear_disc("Reading disc…", f"Drive {letter}:")
        self.monitor.set_drive(letter)

    def on_disc_changed(self, letter, present, toc):
        if letter != self.drive_box.currentData():
            return                               # stale result from a previous drive
        announce = self.first_check_done
        self.first_check_done = True
        if not present:
            self.on_disc_ejected()
            return
        if toc is None:
            self.toc = None
            self.clear_disc("No audio disc", f"Drive {letter}: is not readable as an audio CD")
            return
        self.load_toc(letter, toc)
        if announce:                             # a disc was inserted while the app was running
            self.bring_to_front()

    def on_disc_ejected(self):
        self.toc = None
        self.stop()                              # stops playback and resets the seek bar
        self.order, self.pos = [], 0
        self.clear_disc("No disc", "Insert an audio CD")

    def clear_disc(self, title, subtitle):
        self.current_key = None                  # invalidate any metadata still in flight
        self.tracks = []
        self.list.clear()
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.set_cover(default_raw())
        self.set_backdrop(None)
        self.apply_theme(DEFAULT_PALETTE)
        self.seek.setRange(0, 1)
        self.t_cur.setText("0:00")
        self.t_tot.setText("0:00")

    def load_toc(self, letter, toc):
        self.stop()
        self.drive = letter
        self.toc = toc
        durs = track_durations(toc)
        self.tracks = [{"num": n, "dur": durs[n], "title": f"Track {n:02d}"} for n in toc["audio"]]
        self.album, self.artist = "Audio CD", f"{len(self.tracks)} tracks"
        self.current_key = musicbrainz_disc_id(toc)
        print("MusicBrainz disc ID:", self.current_key)   # visible when run from a terminal
        self.set_cover(default_raw())
        self.set_backdrop(None)
        self.apply_theme(DEFAULT_PALETTE)
        self.rebuild_order(0)
        self.refresh_list()
        self.show_current()
        threading.Thread(target=self._meta_worker,
                         args=(self.current_key, toc, len(self.tracks)), daemon=True).start()

    def _meta_worker(self, key, toc, n):
        try:
            meta = fetch_metadata(toc, n)
        except Exception:
            meta = None
        self.bridge.meta.emit((key, meta))

    def apply_meta(self, payload):
        key, meta = payload
        # ignore results for a disc that has since been ejected or replaced
        if key != self.current_key or not meta or not self.tracks:
            return
        self.album = meta["album"] or self.album
        self.artist = meta["artist"] or self.artist
        for t, title in zip(self.tracks, meta["titles"]):
            if title:
                t["title"] = title
        if meta["art"]:
            img = QImage.fromData(meta["art"])
            if not img.isNull():
                self.set_cover(QPixmap.fromImage(img))
                self.set_backdrop(img)
                self.apply_theme(palette_from_image(img))
        self.refresh_list()
        self.show_current()

    def refresh_list(self):
        self.list.clear()
        for t in self.tracks:
            self.list.addItem(f"{t['num']:02d}    {t['title']}    ·  {fmt_time(t['dur'])}")
        if self.tracks:
            self.list.setCurrentRow(self.order[self.pos])

    def show_current(self):
        if not self.tracks:
            return
        t = self.tracks[self.order[self.pos]]
        self.title.setText(t["title"])
        self.subtitle.setText(f"{self.artist} — {self.album}" if self.artist else self.album)
        self.seek.setRange(0, max(1, t["dur"]))
        self.t_tot.setText(fmt_time(t["dur"]))
        self.list.setCurrentRow(self.order[self.pos])

    # ---- Order / shuffle / repeat ------------------------------------------ #

    def rebuild_order(self, current_idx):
        n = len(self.tracks)
        order = list(range(n))
        if self.shuffle and n:
            random.shuffle(order)
            order.remove(current_idx)
            order.insert(0, current_idx)
            self.pos = 0
        else:
            self.pos = current_idx
        self.order = order

    def toggle_shuffle(self):
        self.shuffle = self.b_shuffle.isChecked()
        if self.tracks:
            self.rebuild_order(self.order[self.pos])

    def cycle_repeat(self):
        self.repeat = (self.repeat + 1) % 3
        self.b_repeat.setChecked(self.repeat != 0)
        self.b_repeat.setText("⟳¹" if self.repeat == 2 else "⟳")
        self.b_repeat.setToolTip(["Repeat: off", "Repeat: all", "Repeat: one"][self.repeat])

    # ---- Playback ---------------------------------------------------------- #

    def play_pos(self, pos):
        if not (self.mp and self.tracks):
            return
        self.pos = pos
        t = self.tracks[self.order[pos]]
        media = self.vlc.media_new(f"cdda:///{self.drive}:/")
        media.add_option(f":cdda-track={t['num']}")
        self.mp.set_media(media)
        self.mp.play()
        self.playing = True
        self.started_at = time.monotonic()
        self.b_play.setText("⏸")
        self.vinyl.set_progress(0)
        self.vinyl.set_state(True, self.rate)
        self.show_current()
        QTimer.singleShot(400, self.apply_rate)

    def toggle_play(self):
        if not self.tracks:
            return
        if not self.playing and self.mp.get_state() in (vlc.State.Paused,):
            self.mp.set_pause(0)
            self.playing = True
            self.b_play.setText("⏸")
        elif self.playing:
            self.mp.set_pause(1)
            self.playing = False
            self.b_play.setText("▶")
        else:
            self.play_pos(self.pos)
        self.vinyl.set_state(self.playing, self.rate)

    def stop(self):
        if self.mp:
            self.mp.stop()
        self.playing = False
        self.b_play.setText("▶")
        self.seek.setValue(0)
        self.t_cur.setText("0:00")
        self.vinyl.set_state(False, self.rate)
        self.vinyl.set_progress(0)

    def next_track(self, auto=False):
        if not self.tracks:
            return
        if auto and self.repeat == 2:
            self.play_pos(self.pos)
        elif self.pos + 1 < len(self.order):
            self.play_pos(self.pos + 1)
        elif self.repeat == 1:
            if self.shuffle:
                random.shuffle(self.order)
            self.play_pos(0)
        else:
            self.stop()
            self.pos = 0
            self.show_current()

    def prev_track(self):
        if not self.tracks:
            return
        if self.playing and self.mp.get_time() > 3000:
            self.mp.set_time(0)
        elif self.pos > 0:
            self.play_pos(self.pos - 1)
        elif self.repeat == 1:
            self.play_pos(len(self.order) - 1)
        else:
            self.mp.set_time(0)

    def on_list_activate(self, item):
        idx = self.list.row(item)
        self.play_pos(self.order.index(idx))

    # ---- Seek / speed / tick ----------------------------------------------- #

    def on_seek_value(self, v):
        if self.seeking:
            self.t_cur.setText(fmt_time(v))

    def on_seek_release(self):
        if self.mp and self.tracks:
            self.mp.set_time(int(self.seek.value()))
        self.seeking = False

    def on_speed(self, v):
        self.rate = v / 100.0
        self.speed_lbl.setText(f"{self.rate:.2f}×")
        self.vinyl.set_state(self.playing, self.rate)
        self.apply_rate()

    def apply_rate(self):
        if self.mp:
            self.mp.set_rate(self.rate)

    def tick(self):
        if not (self.mp and self.tracks and self.playing):
            return
        if time.monotonic() - self.started_at < 0.8:
            return
        t = self.tracks[self.order[self.pos]]
        cur = max(0, self.mp.get_time())
        self.vinyl.set_progress(cur / max(1, t["dur"]))
        if not self.seeking:
            self.seek.setValue(min(cur, self.seek.maximum()))
            self.t_cur.setText(fmt_time(cur))
        if abs(self.mp.get_rate() - self.rate) > 0.01:
            self.apply_rate()
        if self.mp.get_state() == vlc.State.Ended or (t["dur"] and cur >= t["dur"] - 250):
            self.next_track(auto=True)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DiscJockey")
    app.setFont(QFont("Segoe UI", 10))
    app.setQuitOnLastWindowClosed(False)  # the tray keeps the app alive
    w = Player()
    disc_watch = DiscArrivalFilter(w.on_disc_inserted)
    app.installNativeEventFilter(disc_watch)
    if "--background" not in sys.argv:
        w.bring_to_front()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()