from __future__ import annotations

import ssl
from contextlib import asynccontextmanager
from enum import Enum
from typing import AsyncContextManager

import anyio
from anyio import Event as AioEvent, CancelScope, ClosedResourceError, EndOfStream
from anyio import create_memory_object_stream
from anyio._core._synchronization import ResourceGuard
from anyio.abc import SocketStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.streams.tls import TLSStream
from furl import furl
from wsproto import WSConnection, ConnectionType
from wsproto.events import Request, Event, CloseConnection, TextMessage, BytesMessage, \
    RejectConnection, RejectData, AcceptConnection, Ping, Pong

from stickney.exc import WebsocketStateMachineError, ConnectionRejectedError, WebsocketClosedError
from stickney.frame import WsMessage, CloseMessage, RejectMessage, TextualMessage, BinaryMessage, \
    ConnectionOpenMessage, PingMessage, PongMessage


class BufferType(Enum):
    """
    Enumeration of the possible messages that are being buffered.
    """

    #: The current message being buffered is the body of a rejected upgrade response.
    REJECT = 0

    #: The current message being buffered is a regular WebSocket text message.
    TEXTUAL = 1

    #: The current message being buffered is a regular WebSocket bytes message.
    BYTES = 2


class WebsocketClient(object):
    """
    A single client websocket connection.
    """

    BUFFER_SIZE = 2 ** 16

    def __init__(
        self,
        sock: SocketStream | TLSStream,
        *,
        graceful_closes: bool = True,
        cancel_scope: CancelScope,
        buffer_size: float = 0,
    ):
        self._sock = sock
        self._gracefully_close = graceful_closes
        self._proto = WSConnection(ConnectionType.CLIENT)
        self._cancel_scope = cancel_scope

        self._lock = anyio.Lock()
        self._recv_lock = ResourceGuard("receiving websocket messages from")

        self._close_event = AioEvent()
        self._open_event = AioEvent()

        # Closure is a bit messy; websockets can be sorta half-closed.
        # ``_is_closing``: somebody, somewhere, has requested the websocket needs to be closed.
        # messages can be still be sent and received.
        self._is_closing = False
        # ``is_definitely_closed``: The WS is *definitely* closed; no messages can be sent or
        # received.
        self._is_definitely_closed = False
        # ``client_initialised_close``: Don't bother responding to the server's close frame.
        self._client_initialised_close = False

        write, read = create_memory_object_stream(buffer_size)
        self._read_incoming: MemoryObjectReceiveStream[WsMessage] = read
        self._write_incoming: MemoryObjectSendStream[WsMessage] = write

        # used for buffering various message types, last buf message is shared across all of them
        # but rejected error code is only used for rejected messages.
        self._buffer_type: BufferType | None = None
        self._rejected_error_code: int = 0
        self._last_buffered_message: str | bytes = ""

        self._close_code: int = 0
        self._close_reason: str = ""

        self._pumping = False

    async def _send_message(self, event: Event):
        """
        Sends a single message over the websocket connection.
        """

        if self._is_definitely_closed:
            raise WebsocketClosedError(self._close_code, self._close_reason)

        async with self._lock:
            data = self._proto.send(event)
            await self._sock.send(data)

    async def _do_handshake(self, url: furl):
        """
        Does the opening websocket handshake.
        """

        path = str(url.path) if url.path else "/"

        if url.query:
            req = Request(host=url.host, target=path + "?" + str(url.query))
        else:
            req = Request(host=url.host, target=path)
        await self._sock.send(self._proto.send(req))

        incoming = await self.receive_single_message()
        if not isinstance(incoming, ConnectionOpenMessage):
            raise WebsocketStateMachineError(f"Expected a ConnectionOpenMessage, got a {incoming}")

    async def _process_single_inbound_event(self, event: Event):
        if isinstance(event, AcceptConnection):
            await self._write_incoming.send(ConnectionOpenMessage())

        elif isinstance(event, CloseConnection):
            self._close_event.set()

            self._is_closing = True
            self._close_reason = event.reason
            self._close_code = event.code

            try:
                if not (self._is_definitely_closed or self._client_initialised_close):
                    try:
                        await self._send_message(event.response())
                    except (ClosedResourceError, EndOfStream, WebsocketClosedError):
                        # ok, thanks
                        pass
            finally:
                self._is_definitely_closed = True
                self._close_event.set()
                await self._write_incoming.send(
                    CloseMessage(close_code=event.code, reason=event.reason)
                )
                self._write_incoming.close()
                await self._sock.aclose()

        elif isinstance(event, RejectConnection):
            assert self._buffer_type is None, "shouldnt get a reject during buffering, wtf wsproto?"
            self._buffer_type = BufferType.REJECT
            self._rejected_error_code = event.status_code
            self._last_buffered_message = b""
            self._is_closing = True

            if not event.has_body:
                try:
                    await self._write_incoming.send(
                        RejectMessage(status_code=self._rejected_error_code)
                    )
                finally:
                    self._is_closed = True
                    self._close_event.set()

                    await self._sock.aclose()

        elif isinstance(event, RejectData):
            assert self._buffer_type == BufferType.REJECT, "reject data before reject conn? wtf??"

            self._last_buffered_message += event.data
            if event.body_finished:
                try:
                    await self._write_incoming.send(
                        RejectMessage(
                            status_code=self._rejected_error_code, body=self._last_buffered_message
                        )
                    )
                finally:
                    self._is_closed = True
                    self._close_event.set()

                    await self._sock.aclose()

        elif isinstance(event, TextMessage):
            if self._buffer_type == BufferType.TEXTUAL:
                self._last_buffered_message += event.data
            elif self._buffer_type is None:
                self._buffer_type = BufferType.TEXTUAL
                self._last_buffered_message = event.data
            else:
                raise WebsocketStateMachineError(
                    "Buffer type should either be textual or None when reading a textual "
                    f"message, not {self._buffer_type}!"
                )

            if event.message_finished:
                self._buffer_type = None
                body = self._last_buffered_message
                self._last_buffered_message = ""
                await self._write_incoming.send(TextualMessage(body))

        elif isinstance(event, BytesMessage):
            if self._buffer_type == BufferType.BYTES:
                self._last_buffered_message += event.data
            elif self._buffer_type is None:
                self._buffer_type = BufferType.BYTES
                self._last_buffered_message = event.data
            else:
                raise WebsocketStateMachineError(
                    "Buffer type should either be bytes or None when reading a binary "
                    f"message, not {self._buffer_type}!"
                )

            if event.message_finished:
                self._buffer_type = None
                body = self._last_buffered_message
                self._last_buffered_message = b""
                await self._write_incoming.send(BinaryMessage(body))

        elif isinstance(event, Ping):
            if not self._is_definitely_closed:
                await self._send_message(event.response())

            await self._write_incoming.send(PingMessage(event.payload))

        elif isinstance(event, Pong):
            # kill yourself?
            await self._write_incoming.send(PongMessage(event.payload))

    async def _pump_messages(self):
        while not self._is_definitely_closed:
            try:
                raw_data = await self._sock.receive(WebsocketClient.BUFFER_SIZE)
            except (ClosedResourceError, EndOfStream):
                # EOF means that we are *absolutely* closed and there's nothing to do anymore.
                self._is_definitely_closed = True
                await self._process_single_inbound_event(
                    CloseConnection(1006, "Socket unexpectedly closed")
                )
                self._write_incoming.close()
                return

            self._proto.receive_data(raw_data)

            for event in self._proto.events():
                await self._process_single_inbound_event(event)

    @property
    def definitely_closed(self) -> bool:
        return self._is_definitely_closed

    async def receive_single_message(self) -> WsMessage:
        """
        Receives a single message from the websocket connection.
        """

        if self._is_definitely_closed:
            raise WebsocketClosedError(self._close_code, self._close_reason)

        with self._recv_lock:
            msg = await self._read_incoming.receive()

            if isinstance(msg, RejectMessage):
                raise ConnectionRejectedError(msg.status_code, msg.body)

            elif isinstance(msg, CloseMessage) and not self._client_initialised_close:
                raise WebsocketClosedError(msg.close_code, msg.reason)

            return msg

    async def close(self, *, code: int = 1000, reason: str = "Normal close"):
        """
        Closes the websocket connection. If the websocket is already closed (or is closing),
        this method does nothing.

        :param code: The closing code to use. This should be in the range 1000 .. 1011 for most
                     applications.
        :param reason: The close reason to use. Arbitrary string.
        """

        self._is_closing = True

        if self._is_definitely_closed:
            # on trio, at least, this just calls checkpoint().
            return await anyio.sleep(0)

        self._close_code = code
        self._close_reason = reason

        self._client_initialised_close = True
        event = CloseConnection(code=code, reason=reason)

        try:
            await self._sock.send(self._proto.send(event))
            # drain all other incoming events
            while not self._is_definitely_closed:
                await self.receive_single_message()

        finally:
            self._cancel_scope.cancel()
            await self._sock.aclose()

    async def send_ping(self, data: bytes = b"Have you heard of the high elves?"):
        """
        Sends a Ping frame to the remote server.

        :param data: Arbitrary application data. This will be returned from the server in a Pong
                     message.
        """

        await self._send_message(Ping(data))

    async def send_message(self, data: str | bytes, *, message_finished: bool = True):
        """
        Sends a message to the remote websocket server.

        :param data: The data to send. This may be either ``str`` or ``bytes``; the type will change
                     the underlying message frame type.
        :param message_finished: If this is a complete message or not.
        """

        if isinstance(data, str):
            event = TextMessage(data, message_finished=message_finished)
        elif isinstance(data, bytes):
            event = BytesMessage(data, message_finished=message_finished)
        else:
            raise TypeError(f"Expected data to be either str or bytes, not ``{type(data)}")

        await self._send_message(event)


def open_ws_connection(
    url: str,
    *,
    gracefully_close: bool = True,
    tls_context: ssl.SSLContext = None
) -> AsyncContextManager[WebsocketClient]:
    """
    Opens a new websocket connection to the provided URL, and returns a new asynchronous context
    manager holding the websocket.

    If ``tls_context`` is unset, but the URL provided is ``https`` or ``wss``, then a default
    one will be created instead.

    :param url: The URL of the websocket endpoint to connect to.
    :param gracefully_close: If True, then a graceful close will be attempted.
    :param tls_context: The TLS context to use for opening WSS connections. Optional.
    """

    url = furl(url)

    port: int = url.port
    if port is None:
        # furl handles this, but otherwise just default to port 80
        port = 80

    if (url.scheme == "wss" or url.scheme == "https") and not tls_context:
        tls_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)

    @asynccontextmanager
    async def _do():
        async with (await anyio.connect_tcp(
            url.host, port,
            tls=tls_context is not None,
            ssl_context=tls_context,
            tls_standard_compatible=False,
        )) as socket, anyio.create_task_group() as nursery:
            conn = WebsocketClient(
                socket,
                graceful_closes=gracefully_close,
                cancel_scope=nursery.cancel_scope
            )
            nursery.start_soon(conn._pump_messages)
            await conn._do_handshake(url)

            try:
                yield conn
            finally:
                await conn.close(code=1000)
                # I fucking hate asyncio!
                nursery.cancel_scope.cancel()

    return _do()
