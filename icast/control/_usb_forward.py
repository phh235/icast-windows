"""Own a loopback listener and forward only through the chosen USB device."""
import asyncio
import os
import sys

from pymobiledevice3.tcp_forwarder import UsbmuxTcpForwarder


async def serve(udid, port):
    listening = asyncio.Event()
    forwarder = UsbmuxTcpForwarder(
        udid, port, 0, listening_event=listening, usbmux_connection_type="USB",
        usbmux_address=os.environ.get("PYMOBILEDEVICE3_USBMUX") or os.environ.get("USBMUXD_SOCKET_ADDRESS"),
    )
    listener = asyncio.create_task(forwarder.start(address="127.0.0.1"))
    ready = asyncio.create_task(listening.wait())
    try:
        await asyncio.wait((listener, ready), return_when=asyncio.FIRST_COMPLETED)
        if listener.done():
            await listener
            raise RuntimeError("USB forward stopped before binding")
        print(f"ICAST_USB_FORWARD_PORT={forwarder.listening_port}", flush=True)
        await listener
    finally:
        ready.cancel()
        listener.cancel()
        await asyncio.gather(ready, listener, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(serve(sys.argv[1], int(sys.argv[2])))
