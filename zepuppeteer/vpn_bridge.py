"""Private TCP-to-UNIX relay run as the desktop user inside a VPN namespace."""

import argparse
import asyncio
import os
from pathlib import Path


async def copy_stream(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def relay(reader, writer, socket_path):
    upstream = None
    try:
        remote, upstream = await asyncio.open_unix_connection(socket_path)
        await asyncio.gather(copy_stream(reader, upstream), copy_stream(remote, writer))
    except (ConnectionError, OSError):
        pass
    finally:
        for stream in (writer, upstream):
            if stream is not None:
                stream.close()
                try:
                    await stream.wait_closed()
                except (ConnectionError, OSError):
                    pass


async def main(args):
    server = await asyncio.start_server(
        lambda reader, writer: relay(reader, writer, args.socket), "127.0.0.1", args.port)
    Path(args.ready).write_text(str(os.getpid()), encoding="ascii")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--ready", required=True)
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        pass
