"""Known-answer tests for the Python FairPlay port.

Vectors: openairplay/airplay2-receiver fp_decrypt.py (20 sessions, modes 0-3; also
used by r4v3n6101/rairplay against the compiled C playfair) and
omarroth/doubletake fairplay_key_test.go (1 session, cross-checked with C playfair).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from icast.fairplay.playfair import playfair_decrypt  # noqa: E402

VECTORS = json.loads((Path(__file__).parent / "fairplay_vectors.json").read_text())


def main():
    ok = 0
    for i, (m3, ekey, key) in enumerate(VECTORS):
        got = playfair_decrypt(bytes.fromhex(m3), bytes.fromhex(ekey)).hex()
        mode = bytes.fromhex(m3)[12]
        status = "OK " if got == key else "BAD"
        ok += got == key
        print(f"{status} #{i:2d} mode {mode}: {got}" + ("" if got == key else f" expected {key}"))
    print(f"{ok}/{len(VECTORS)} passed")
    return 0 if ok == len(VECTORS) else 1


if __name__ == "__main__":
    sys.exit(main())
