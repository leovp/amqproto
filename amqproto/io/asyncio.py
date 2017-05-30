import asyncio
from asyncio.streams import _DEFAULT_LIMIT

from amqproto import protocol
from amqproto.channel import Channel as SansioChannel
from amqproto.connection import Connection as SansioConnection


class Channel(SansioChannel):

    def __init__(self, *args,
                 loop: asyncio.BaseEventLoop,
                 writer,
                 **kwargs):
        self.loop = loop
        self.writer = writer
        # Late call because we need self.Future available.
        super().__init__(*args, **kwargs)

        self._waiters = set()

    def Future(self):
        return self.loop.create_future()

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        reply_code = 0
        reply_text = ''
        if isinstance(exc, protocol.AMQPError):
            reply_code = exc.reply_code
            reply_text = exc.reply_text
        await self.close(reply_code, reply_text)

    def _send_method(self, *args, **kwargs):
        super()._send_method(*args, **kwargs)
        self.writer.write(self.data_to_send())

    async def basic_consume(self, *args, **kwargs):
        future, consumer_tag = await super().basic_consume(*args, **kwargs)
        self._waiters.add(future)
        return consumer_tag

    async def receive_messages(self):
        while self._consumers:
            done, self._waiters = await asyncio.wait(
                self._waiters, loop=self.loop,
                return_when=asyncio.FIRST_COMPLETED
            )
            # done should have a single element, since we're waiting
            # for FIRST_COMPLETED.
            message, future = await next(iter(done))
            consumer_tag = message.delivery_info.consumer_tag
            # Return this future to the consumers set so we can consume
            # from the same queue again.
            self._waiters.add(future)
            yield message


class Connection(SansioConnection):

    def __init__(self, host='localhost', port=5672, *,
                 limit=_DEFAULT_LIMIT, ssl=None, family=0, proto=0,
                 flags=0, sock=None, local_addr=None, server_hostname=None,
                 loop: asyncio.BaseEventLoop, **kwargs):
        self.loop = loop
        # Late call because we need self.Future available.
        super().__init__(**kwargs)

        async def open_connection():
            self.reader, self.writer = await asyncio.open_connection(
                host=host, port=port, loop=loop, limit=limit,
                ssl=ssl, family=family, proto=proto, flags=flags,
                sock=sock, local_addr=local_addr,
                server_hostname=server_hostname,
            )
            loop.create_task(self._communicate())
        self._open_connection = open_connection

    def Future(self):
        return self.loop.create_future()

    def Channel(self, *args, **kwargs):
        return Channel(*args, loop=self.loop, writer=self.writer, **kwargs)

    async def _communicate(self):
        data = b''
        reader, writer = self.reader, self.writer
        while self.alive:
            writer.write(self.data_to_send())
            await writer.drain()
            frame_max = self.properties['frame_max']
            data += await reader.read(10 * frame_max)
            frames = self.receive_frames(data)
            for frame in frames:
                self.handle_frame(frame)
                data = data[frame_max:]

    async def __aenter__(self):
        await self._open_connection()
        await self.initiate_connection()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        reply_code = 0
        reply_text = ''
        if isinstance(exc, protocol.AMQPError):
            reply_code = exc.reply_code
            reply_text = exc.reply_text
        await self.close(reply_code, reply_text)

    def _send_method(self, *args, **kwargs):
        super()._send_method(*args, **kwargs)
        self.writer.write(self.data_to_send())