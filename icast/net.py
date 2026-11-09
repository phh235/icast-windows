"""Network binding options (overridden by --local for self-tests)."""
BIND_HOST = "0.0.0.0"
ADVERTISE = True
PORT = 7000  # RTSP control port; falls back to a random free port if busy
