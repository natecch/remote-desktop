"""Direct peer-to-peer link (WebRTC data channel), like TeamViewer's direct connections.

A viewer first connects the usual way (the internet tunnel or the local network). Once signed
in it offers a WebRTC connection over that link; this side answers. When the two computers can
reach each other directly (STUN finds each side's public address and both punch a hole through
their routers), the viewer signs in again over the data channel and closes the first link, so
the picture no longer passes through Cloudflare. When they cannot, nothing changes.

`ChannelSocket` makes the data channel look like the websocket that `host.handler` expects.
Messages are split into 16 KB pieces, the size every browser accepts: the first byte of each
piece says whether more pieces follow (bit 0) and whether the message is text (bit 1).
"""

import asyncio

try:
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
except ImportError:  # optional: without it, viewers simply keep the first link
    RTCPeerConnection = None

from websockets.exceptions import ConnectionClosedOK

STUN = ["stun:stun.cloudflare.com:3478", "stun:stun.l.google.com:19302"]
PIECE = 16 * 1024
MORE, TEXT = 1, 2
HIGH_WATER = 1 << 20     # bytes queued before send() waits for the channel to drain
MAX_MESSAGE = 4 << 20    # largest message accepted from the viewer
MAX_LINKS = 8            # direct links being set up or open at once
OFFER_TIMEOUT = 20       # seconds for the direct link to open before it is dropped

PEERS = set()


def available():
    return RTCPeerConnection is not None


class ChannelSocket:
    """Enough of a websockets connection for host.handler: send, recv, iteration, close."""

    def __init__(self, pc, channel, ip):
        self.pc, self.channel = pc, channel
        self.remote_address = (ip, 0)
        self.request = None
        self.inbox = asyncio.Queue()
        self.parts = []
        self.size = 0
        self.closed = asyncio.Event()
        self.drained = asyncio.Event()
        self.drained.set()
        channel.bufferedAmountLowThreshold = HIGH_WATER // 2
        channel.on("message", self._on_message)
        channel.on("close", self._on_close)
        channel.on("bufferedamountlow", self.drained.set)

        @pc.on("connectionstatechange")
        def _state():
            if pc.connectionState in ("failed", "closed"):
                self._on_close()

    def _on_message(self, data):
        if isinstance(data, str) or not data:
            return
        self.parts.append(data[1:])
        self.size += len(data) - 1
        if self.size > MAX_MESSAGE:  # a misbehaving viewer: drop the link
            asyncio.ensure_future(self.close())
            return
        if data[0] & MORE:
            return
        body = b"".join(self.parts)
        self.parts, self.size = [], 0
        if data[0] & TEXT:
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError:
                return
        self.inbox.put_nowait(body)

    def _on_close(self):
        if not self.closed.is_set():
            self.closed.set()
            self.inbox.put_nowait(None)
            self.drained.set()
            asyncio.ensure_future(self.pc.close())

    async def send(self, message):
        if self.closed.is_set() or self.channel.readyState != "open":
            raise ConnectionClosedOK(None, None)
        if isinstance(message, str):
            body, flag = message.encode("utf-8"), TEXT
        else:
            body, flag = bytes(message), 0
        last = max(len(body) - 1, 0) // PIECE
        for i in range(last + 1):
            head = bytes([flag | (MORE if i < last else 0)])
            self.channel.send(head + body[i * PIECE:(i + 1) * PIECE])
        while self.channel.bufferedAmount > HIGH_WATER and not self.closed.is_set():
            self.drained.clear()
            try:
                await asyncio.wait_for(self.drained.wait(), 1)
            except asyncio.TimeoutError:
                pass
        if self.closed.is_set():
            raise ConnectionClosedOK(None, None)

    async def recv(self):
        message = await self.inbox.get()
        if message is None:
            self.inbox.put_nowait(None)  # later callers see the close too
            raise ConnectionClosedOK(None, None)
        return message

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except ConnectionClosedOK:
            raise StopAsyncIteration

    async def close(self, code=1000, reason=""):
        try:
            self.channel.close()
        except Exception:
            pass
        self._on_close()


async def answer(offer_sdp, ip, handler):
    """Answer a viewer's offer. When its data channel opens, run `handler` on it (the same
    sign-in and session as a websocket). Returns the answer SDP, or None."""
    if not available() or len(PEERS) >= MAX_LINKS or not isinstance(offer_sdp, str) or len(offer_sdp) > 20000:
        return None
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=STUN)]))
    PEERS.add(pc)
    opened = asyncio.Event()

    @pc.on("datachannel")
    def _channel(channel):
        if opened.is_set() or channel.label != "rd":
            return
        opened.set()
        sock = ChannelSocket(pc, channel, ip)

        async def run():
            try:
                await handler(sock)
            finally:
                await sock.close()
                PEERS.discard(pc)
        asyncio.ensure_future(run())

    async def expire():  # never keep a half-made link
        await asyncio.sleep(OFFER_TIMEOUT)
        if not opened.is_set():
            await pc.close()
            PEERS.discard(pc)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())  # waits for this side's addresses
    except Exception:
        await pc.close()
        PEERS.discard(pc)
        return None
    asyncio.ensure_future(expire())
    return pc.localDescription.sdp


async def close_all():
    for pc in list(PEERS):
        await pc.close()
    PEERS.clear()


async def _loopback():
    """Two peers on this computer exchange a large message through ChannelSocket."""
    a = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    b = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    channel = a.createDataChannel("rd")
    got = asyncio.get_running_loop().create_future()

    @b.on("datachannel")
    def _channel(remote):
        sock = ChannelSocket(b, remote, "test")

        async def echo():
            got.set_result(await sock.recv())
        asyncio.ensure_future(echo())

    opened = asyncio.Event()
    channel.on("open", opened.set)
    try:
        await a.setLocalDescription(await a.createOffer())
        await b.setRemoteDescription(a.localDescription)
        await b.setLocalDescription(await b.createAnswer())
        await a.setRemoteDescription(b.localDescription)
        await asyncio.wait_for(opened.wait(), 15)
        sender = ChannelSocket(a, channel, "test")
        payload = bytes(range(256)) * 400  # 100 KB: several pieces
        await sender.send(payload)
        echoed = await asyncio.wait_for(got, 15)
        if echoed != payload:
            raise RuntimeError("message arrived damaged")
        return f"{len(payload)} bytes"
    finally:
        await a.close()
        await b.close()


def selftest():
    if not available():
        raise ImportError("aiortc is not bundled")
    return asyncio.run(_loopback())
