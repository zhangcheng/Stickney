import pytest
from wsproto.events import CloseConnection, TextMessage

from stickney.client import open_ws_connection
from stickney.exc import WebsocketClosedError, ConnectionRejectedError
from stickney.frame import TextualMessage, BinaryMessage
from stickney.sim import SimulatedWebsocket

pytestmark = pytest.mark.anyio


async def test_normal_client_connection():
    """
    Tests a normal echo connection with a real server.
    """

    async with open_ws_connection("ws://127.0.0.1:1337") as conn:
        await conn.send_message("echo!")
        next_message = await conn.receive_single_message()

        assert isinstance(next_message, TextualMessage)
        assert next_message.body == "echo!"

        await conn.send_message(b"echo!")
        next_message = await conn.receive_single_message()

        assert isinstance(next_message, BinaryMessage)
        assert next_message.body == b"echo!"


async def test_rejected_client_connection():
    with pytest.raises(ExceptionGroup) as e:
        async with open_ws_connection("wss://google.co.uk/"):
            pass

    assert isinstance(e.value.exceptions[0], ConnectionRejectedError)


async def test_act_sanely_during_socket_close():
    """
    Ensures that the client acts mostly sane during a socket close.
    """

    async with open_ws_connection("ws://127.0.0.1:1337") as conn:
        await conn._sock.aclose()

        with pytest.raises(WebsocketClosedError):
            await conn.receive_single_message()


async def test_server_side_close():
    """
    Tests responding correctly to a server-side close.
    """

    # websockets should respond to a server-side close by sending another close frame and then
    # closing the socket.

    ws = SimulatedWebsocket()
    await ws.push_message(CloseConnection(code=1000))

    with pytest.raises(WebsocketClosedError):
        await ws.receive_single_message()

    close_resp = ws.outbound_messages[0]
    assert isinstance(close_resp, CloseConnection)
    assert ws.definitely_closed


async def test_message_buffering():
    ws = SimulatedWebsocket()
    await ws.push_message(TextMessage(data="us", message_finished=False))
    await ws.push_message(TextMessage(data="agi", message_finished=True))

    next = await ws.receive_single_message()
    assert isinstance(next, TextualMessage)
    assert next.body == "usagi"
