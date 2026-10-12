"""Snapshot SUNAPI: si la camara responde 604 (sin perfil MJPEG) se busca uno y se reintenta con Profile=N."""
import cv2
import numpy as np

from app.capture.snapshot_grabber import SnapshotGrabber

JPG = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))[1].tobytes()


class Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code, self.text = content, status, content.decode("latin1")


class Sess:
    def __init__(self, profiles):
        self.profiles, self.calls = profiles, []

    def get(self, url, timeout=6):
        self.calls.append(url)
        if "videoprofile" in url:
            return Resp(self.profiles.encode())
        if "Profile=3" in url:
            return Resp(JPG)
        return Resp(b"NG\r\nError Code: 604\r\nError Details: \r\nInvalid Input Value(s)")


def grabber(profiles):
    g = SnapshotGrabber("cam-191", "rtsp://u:p@10.0.0.9/profile2/media.smp")
    g._sess = Sess(profiles)
    return g


def test_con_perfil_mjpeg_reintenta_y_lo_recuerda():
    g = grabber("Channel.0.Profile.2.EncodingType=H264\nChannel.0.Profile.3.EncodingType=MJPEG\n")
    assert g.grab() is not None and g._profile == 3
    n = len(g._sess.calls)
    assert g.grab() is not None and len(g._sess.calls) == n + 1 and "Profile=3" in g._sess.calls[-1]      # ya no vuelve a descubrir


def test_sin_perfil_mjpeg_falla_como_antes_y_no_insiste():
    g = grabber("Channel.0.Profile.1.EncodingType=H265\nChannel.0.Profile.2.EncodingType=H264\n")
    assert g.grab() is None and g._profile is None
    g.grab()
    assert sum("videoprofile" in c for c in g._sess.calls) == 1
