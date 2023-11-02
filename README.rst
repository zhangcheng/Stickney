Stickney
========

.. image:: https://img.shields.io/pypi/v/stickney
   :alt: PyPI - Version
   :target: https://pypi.org/project/stickney/

Stickney is an asynchronous websockets library for `AnyIO`_ and Python 3.11+. This is primarily
designed for Trio (as asyncio is a bastard evil terrible library that deadlocks constantly) usage.

Installation
------------

Stickney is available on PyPI.

.. code-block:: fish

    $ poetry add stickney@latest

Usage
-----

Create a new websocket with the ``open_ws_connection`` function:

.. code-block:: python

    async with open_ws_connection(url="wss://example.websocket.server/path?a=b") as ws:
        ...

You can send messages with the ``send_message`` function and receive messages with the
``receive_single_message`` function. You can also use ``close``, but the WS is closed automatically
when the context manager exits.

There's not really much else to it. See ``stickney/frames.py`` for the available message types.

Naming
------

Stickney is named after the `Stickney crater`_ on Phobos.

.. _AnyIO: https://anyio.readthedocs.io/en/stable/
.. _Stickney crater: https://en.wikipedia.org/wiki/Stickney_(crater)