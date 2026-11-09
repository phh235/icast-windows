"""Simulated iPhone: drives the receiver through a full mirroring session.

Does what iOS does - GET /info, pair-setup, pair-verify (real Ed25519/X25519),
fp-setup with a recorded FairPlay exchange (tests/fairplay_vectors.json), SETUP of
mirror + audio streams - then streams an encrypted H.264 test pattern and an
encrypted AAC tone, and tears the session down.

Usage:  python tools/fake_iphone.py [host] [port] [seconds]
"""
import hashlib
import json
import math
import plistlib
import socket
import struct
import sys
import time
from pathlib import Path

import av
import numpy as np
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent.parent
RAW = dict(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
W, H = 590, 1280  # portrait, iPhone-like aspect


class Rtsp:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port))
        self.host = host
        self.cseq = 0

    def request(self, method, url, body=b"", ctype=None, headers=None):
        self.cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self.cseq}", "User-Agent: AirPlay/800.1"]
        if ctype:
            lines.append(f"Content-Type: {ctype}")
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        lines.append(f"Content-Length: {len(body)}")
        self.s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.s.recv(1)
            if not chunk:
                raise ConnectionError("closed")
            head += chunk
        text = head.decode()
        status = int(text.split(" ")[1])
        hdrs = {}
        for line in text.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        n = int(hdrs.get("content-length", 0))
        data = b""
        while len(data) < n:
            data += self.s.recv(n - len(data))
        assert status == 200, f"{method} {url} -> {status}"
        assert hdrs.get("cseq") == str(self.cseq), "CSeq mismatch"
        return hdrs, data


def h264_encoder():
    for name in ("libx264", "libopenh264", "h264_mf"):
        try:
            c = av.CodecContext.create(name, "w")
            c.width, c.height = W, H
            c.pix_fmt = "yuv420p"
            c.time_base = __import__("fractions").Fraction(1, 30)
            c.framerate = 30
            c.gop_size = 30
            if name == "libx264":
                c.options = {"preset": "ultrafast", "tune": "zerolatency"}
            c.open()
            return name, c
        except Exception:
            continue
    raise SystemExit("no H.264 encoder available in PyAV")


def split_annexb(data: bytes):
    nals, i, n = [], 0, len(data)
    starts = []
    while i < n - 3:
        if data[i:i + 3] == b"\x00\x00\x01":
            starts.append(i + 3)
            i += 3
        else:
            i += 1
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        nal = data[s:e]
        while nal.endswith(b"\x00"):
            nal = nal[:-1]
        nals.append(nal)
    return nals


def test_frame(i):
    y, x = np.mgrid[0:H, 0:W]
    t = i / 30
    img = np.zeros((H, W, 3), np.uint8)
    img[..., 0] = (127 + 127 * np.sin(x / 60 + t)).astype(np.uint8)
    img[..., 1] = (127 + 127 * np.sin(y / 80 - t * 1.3)).astype(np.uint8)
    img[..., 2] = 180
    cy = int(H / 2 + H / 3 * math.sin(t))
    img[cy - 60:cy + 60, W // 2 - 60:W // 2 + 60] = 255  # moving white square
    return img


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 7000
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 8
    m3, ekey, fp_key = (bytes.fromhex(x) for x in json.loads((ROOT / "tests" / "fairplay_vectors.json").read_text())[3])

    c = Rtsp(host, port)
    _, info = c.request("GET", "/info")
    info = plistlib.loads(info)
    print("info:", info["name"], info["model"], hex(info["features"]), info["displays"][0]["width"], "x", info["displays"][0]["height"])
    server_pk = info["pk"]

    ed = Ed25519PrivateKey.generate()
    ed_pub = ed.public_key().public_bytes(**RAW)
    _, pk = c.request("POST", "/pair-setup", ed_pub, "application/octet-stream")
    assert pk == server_pk, "pair-setup pk mismatch"

    x = X25519PrivateKey.generate()
    x_pub = x.public_key().public_bytes(**RAW)
    _, r = c.request("POST", "/pair-verify", b"\x01\x00\x00\x00" + x_pub + ed_pub, "application/octet-stream")
    server_ecdh, enc_sig = r[:32], r[32:]
    secret = x.exchange(X25519PublicKey.from_public_bytes(server_ecdh))
    key = hashlib.sha512(b"Pair-Verify-AES-Key" + secret).digest()[:16]
    iv = hashlib.sha512(b"Pair-Verify-AES-IV" + secret).digest()[:16]
    ctr = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    sig = ctr.update(enc_sig)
    Ed25519PublicKey.from_public_bytes(server_pk).verify(sig, server_ecdh + x_pub)
    my_sig = ed.sign(x_pub + server_ecdh)
    c.request("POST", "/pair-verify", b"\x00\x00\x00\x00" + ctr.update(my_sig), "application/octet-stream")
    print("pair-verify OK (server signature valid)")

    fp1 = bytes([0x46, 0x50, 0x4C, 0x59, 0x03, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x04, 0x02, 0x00, m3[12], 0xBB])
    _, r = c.request("POST", "/fp-setup", fp1, "application/octet-stream")
    assert len(r) == 142 and r[:4] == b"FPLY" and r[13] == m3[12], "fp-setup 1"
    _, r = c.request("POST", "/fp-setup", m3, "application/octet-stream")
    assert r == bytes.fromhex("46504c590301040000000014") + m3[144:], "fp-setup 2"
    print("fp-setup OK")

    eiv = bytes(range(16))
    setup1 = {
        "ekey": ekey, "eiv": eiv, "timingPort": 50999, "timingProtocol": "NTP",
        "name": "Test iPhone", "model": "iPhone16,1", "osVersion": "26.1", "deviceID": "AA:BB:CC:DD:EE:FF",
        "isScreenMirroringSession": True,
    }
    url = f"rtsp://{host}/1234567890"
    _, r = c.request("SETUP", url, plistlib.dumps(setup1, fmt=plistlib.FMT_BINARY), "application/x-apple-binary-plist")
    print("SETUP 1:", plistlib.loads(r))
    aeskey = hashlib.sha512(fp_key + secret).digest()[:16]

    sid_signed = -0x1234567890ABCDE  # 8-byte bplist ints are signed; receiver must treat as uint64
    sid = sid_signed & 0xFFFFFFFFFFFFFFFF
    streams = {"streams": [{"type": 110, "streamConnectionID": sid_signed, "timestampInfo": []},
                           {"type": 96, "ct": 4, "spf": 1024, "audioFormat": 0x40000, "controlPort": 50998}]}
    _, r = c.request("SETUP", url, plistlib.dumps(streams, fmt=plistlib.FMT_BINARY), "application/x-apple-binary-plist")
    res = {s["type"]: s for s in plistlib.loads(r)["streams"]}
    print("SETUP 2:", res)
    c.request("RECORD", url)
    c.request("SET_PARAMETER", url, b"volume: -144.000000\r\n", "text/parameters")  # muted: test tone stays silent

    # ---- video
    vkey = hashlib.sha512(f"AirPlayStreamKey{sid}".encode() + aeskey).digest()[:16]
    viv = hashlib.sha512(f"AirPlayStreamIV{sid}".encode() + aeskey).digest()[:16]
    vctr = Cipher(algorithms.AES(vkey), modes.CTR(viv)).encryptor()
    vs = socket.create_connection((host, res[110]["dataPort"]))
    enc_name, enc = h264_encoder()
    print("video encoder:", enc_name)

    # ---- audio (AAC-LC tone, AES-CBC per packet)
    a_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    a_addr = (host, res[96]["dataPort"])
    aenc = av.CodecContext.create("aac", "w")
    aenc.sample_rate = 44100
    aenc.layout = "stereo"
    aenc.format = "fltp"
    aenc.open()
    seq, ats, phase = 0, 0, 0.0

    sent_cfg = False
    t0 = time.time()
    n_frames = int(seconds * 30)
    for i in range(n_frames):
        frame = av.VideoFrame.from_ndarray(test_frame(i), format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        for pkt in enc.encode(frame):
            nals = split_annexb(bytes(pkt))
            sps = next((n for n in nals if n[0] & 0x1F == 7), None)
            pps = next((n for n in nals if n[0] & 0x1F == 8), None)
            if sps and pps and not sent_cfg:
                avcc = bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1]) + struct.pack(">H", len(sps)) + sps
                avcc += b"\x01" + struct.pack(">H", len(pps)) + pps
                hdr = bytearray(128)
                struct.pack_into("<I", hdr, 0, len(avcc))
                hdr[4], hdr[6], hdr[7] = 1, 0x16, 0x01
                struct.pack_into("<ff", hdr, 16, W, H)
                struct.pack_into("<ff", hdr, 40, W, H)
                struct.pack_into("<ff", hdr, 56, W, H)
                vs.sendall(bytes(hdr) + avcc)
                sent_cfg = True
            body = b"".join(struct.pack(">I", len(n)) + n for n in nals if n[0] & 0x1F not in (7, 8, 9))
            if not body or not sent_cfg:
                continue
            hdr = bytearray(128)
            struct.pack_into("<I", hdr, 0, len(body))
            hdr[4] = 0
            struct.pack_into("<Q", hdr, 8, int((time.time() + 2208988800) * (1 << 32)))
            vs.sendall(bytes(hdr) + vctr.update(body))
        # ~1/30 s of audio
        samples = np.arange(1470) / 44100
        tone = (0.2 * np.sin(2 * math.pi * 440 * samples + phase)).astype(np.float32)
        phase += 2 * math.pi * 440 * 1470 / 44100
        af = av.AudioFrame.from_ndarray(np.stack([tone, tone]), format="fltp", layout="stereo")
        af.sample_rate = 44100
        for ap in aenc.encode(af):
            payload = bytes(ap)
            n = len(payload) // 16 * 16
            encd = Cipher(algorithms.AES(aeskey), modes.CBC(eiv)).encryptor().update(payload[:n]) + payload[n:]
            rtp = bytes([0x80, 0x60]) + struct.pack(">HII", seq & 0xFFFF, ats, 0x12345678)
            a_sock.sendto(rtp + encd, a_addr)
            seq += 1
            ats += 1024
        delay = t0 + (i + 1) / 30 - time.time()
        if delay > 0:
            time.sleep(delay)
    print(f"streamed {n_frames} frames in {time.time() - t0:.1f}s")
    c.request("TEARDOWN", url, plistlib.dumps({"streams": [{"type": 110}]}, fmt=plistlib.FMT_BINARY),
              "application/x-apple-binary-plist")
    vs.close()
    c.request("TEARDOWN", url)
    print("session finished")


if __name__ == "__main__":
    main()
