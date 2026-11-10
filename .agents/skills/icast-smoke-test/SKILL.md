---
name: icast-smoke-test
description: Run iCast's Windows smoke tests and verify AirPlay mirroring with the bundled iPhone simulator. Use for smoke-test requests or to verify changes to receiver startup, AirPlay, video, or audio.
---

# iCast Smoke Test

Run from the iCast repository root. Prefer the installed Python at `%LOCALAPPDATA%\iCast\venv\Scripts\python.exe` or an existing Python 3.11+ development environment. Resolve the interpreter before overriding any environment variables for the test. The simulator needs NumPy. Use a development environment for simulator dependencies so the mirror-only installation stays lean.

## Automated checks

Run these with the selected interpreter. Use the relevant subset if the request calls for a targeted check.

~~~powershell
python -m unittest discover -s tests -q
python tests/test_fairplay.py
git diff --check
~~~

## Loopback mirroring

1. Create a temporary directory for the run. Set the receiver child's `LOCALAPPDATA` to that directory to isolate its settings and logs. Set `QT_QPA_PLATFORM=offscreen` for a headless run.
2. Start the receiver in a separate process and capture its output. Replace `<capture-dir>` with a quoted path inside the temporary directory.

   ~~~powershell
   python -m icast --local --grab "<capture-dir>" --quit-after 20
   ~~~

3. Wait for `RTSP server listening on port` in the new receiver log. Use the reported port because iCast can fall back to another port. Run the simulator for six seconds with the same Python environment.

   ~~~powershell
   python tools/fake_iphone.py 127.0.0.1 <port> 6
   ~~~

4. Check that the simulator exits successfully and prints `session finished`, that a captured PNG shows the simulated video pattern, and that the receiver logs `audio decoding OK`. `--local` mutes playback, so report audio decoding separately from speaker output. Check that the receiver exits cleanly.
5. On failure, stop only the receiver or simulator process you launched if it is still running. Retain the relevant logs and report the failing or skipped check separately from passing checks. Keep test artifacts outside the source tree.

Report which checks ran and whether the video pattern and audio decode were observed. Treat real-device mirroring and USB/WDA as separate checks.
