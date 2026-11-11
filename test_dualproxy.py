import asyncio
import hashlib
import os
import socket
import unittest

import dualproxy

SECRET = b"test-secret-0123456789"
SOURCES = ["127.0.0.1", "127.0.0.2"]
PAYLOAD = os.urandom(32 * 1024 * 1024)


async def send_payload(reader, writer):
    writer.write(PAYLOAD)
    await writer.drain()
    writer.close()


async def echo(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    writer.close()


async def socks_connect(port, target_port, host=b"127.0.0.1"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    assert await reader.readexactly(2) == b"\x05\x00"
    writer.write(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + target_port.to_bytes(2, "big"))
    await writer.drain()
    reply = await reader.readexactly(10)
    return reader, writer, reply[1]


class ProxyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = dualproxy.Server(SECRET, "0.0.0.0", 0)
        await self.server.start()
        self.client = dualproxy.Client("127.0.0.1", SECRET, SOURCES,
                                       server_port=self.server.port, listen_port=0)
        await self.client.start()
        await self.wait_for_links(4)
        self.targets = []

    async def asyncTearDown(self):
        await self.client.stop()
        await self.server.stop()
        for target in self.targets:
            target.close()

    async def wait_for_links(self, count):
        for _ in range(200):
            if len(self.client.active) >= count:
                return
            await asyncio.sleep(0.01)
        self.fail(f"リンクが{count}本になりません: {len(self.client.active)}")

    async def start_target(self, handler):
        target = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.targets.append(target)
        return target.sockets[0].getsockname()[1]

    async def fetch(self, port):
        reader, writer, code = await socks_connect(self.client.listen_port, port)
        self.assertEqual(code, 0)
        data = await asyncio.wait_for(reader.read(-1), 60)
        writer.close()
        return data

    async def test_download_is_intact_and_uses_both_sources(self):
        port = await self.start_target(send_payload)
        data = await self.fetch(port)
        self.assertEqual(hashlib.sha256(data).digest(), hashlib.sha256(PAYLOAD).digest())
        totals = self.client.received_by_source()
        for source in SOURCES:
            self.assertGreater(totals[source], 0)

    async def test_upload_and_half_close(self):
        port = await self.start_target(echo)
        reader, writer, code = await socks_connect(self.client.listen_port, port)
        self.assertEqual(code, 0)
        body = PAYLOAD[:8 * 1024 * 1024]

        async def upload():
            writer.write(body)
            await writer.drain()
            writer.write_eof()

        task = asyncio.create_task(upload())
        data = await asyncio.wait_for(reader.read(-1), 60)
        await task
        writer.close()
        self.assertEqual(data, body)

    async def test_concurrent_streams(self):
        port = await self.start_target(send_payload)
        results = await asyncio.gather(*(self.fetch(port) for _ in range(4)))
        for data in results:
            self.assertEqual(data, PAYLOAD)

    async def test_survives_link_failure_during_transfer(self):
        port = await self.start_target(send_payload)
        task = asyncio.create_task(self.fetch(port))
        await asyncio.sleep(0.03)
        for link in [l for l in self.client.active if l.label == SOURCES[1]]:
            link.writer.transport.abort()
        data = await task
        self.assertEqual(hashlib.sha256(data).digest(), hashlib.sha256(PAYLOAD).digest())
        await self.wait_for_links(4)  # 切れたリンクは張り直される

    async def test_path_recovers_after_unexpected_error(self):
        # 想定外の例外で経路の張り直しが止まると、その経路は二度と使われない (黙って片肺になる)
        original = self.client.connect_link
        failed = []

        async def flaky(source):
            if source == SOURCES[1] and not failed:
                failed.append(source)
                raise RuntimeError("injected")
            return await original(source)

        self.client.connect_link = flaky
        for link in [l for l in self.client.active if l.label == SOURCES[1]]:
            link.writer.transport.abort()
        for _ in range(400):
            if failed and sum(l.label == SOURCES[1] for l in self.client.active) == 2:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(failed)
        self.assertEqual(sum(l.label == SOURCES[1] for l in self.client.active), 2)

    async def test_auto_detected_path_keeps_retrying_after_failures(self):
        # 自動検出モードは経路を後から渡す。最初の接続失敗で経路のタスクが消えてはいけない
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        client = dualproxy.Client("127.0.0.1", SECRET, [], server_port=closed_port,
                                  listen_port=0, auto_sources=True)
        client.sources = [SOURCES[0]]
        await client.start()
        try:
            await asyncio.sleep(2.5)
            tasks = client.source_tasks[SOURCES[0]]
            self.assertTrue(all(not t.done() for t in tasks))
            self.assertGreaterEqual(client.unusable[SOURCES[0]], 2)
        finally:
            await client.stop()

    async def test_survives_server_side_link_failure(self):
        port = await self.start_target(send_payload)
        task = asyncio.create_task(self.fetch(port))
        await asyncio.sleep(0.03)
        mux = next(iter(self.server.sessions.values()))
        next(iter(mux.links)).writer.transport.abort()
        self.assertEqual(await task, PAYLOAD)

    async def test_all_streams_survive_path_loss(self):
        """1経路が丸ごと落ちても、走っていた全ストリームが完走する (取り出し済みフレームの復還)。"""
        port = await self.start_target(send_payload)
        tasks = [asyncio.create_task(self.fetch(port)) for _ in range(4)]
        await asyncio.sleep(0.05)
        for link in [l for l in self.client.active if l.label == SOURCES[1]]:
            link.writer.transport.abort()
        for task in tasks:
            data = await task
            self.assertEqual(hashlib.sha256(data).digest(), hashlib.sha256(PAYLOAD).digest())
        await self.wait_for_links(4)  # 切れた経路は張り直される

    async def test_aborted_app_connection_leaves_no_stall(self):
        """転送途中にアプリが閉じても、後の接続は詰まらない (キューにフレームを残さない)。"""
        port = await self.start_target(send_payload)
        reader, writer, code = await socks_connect(self.client.listen_port, port)
        self.assertEqual(code, 0)
        await asyncio.sleep(0.05)
        writer.close()  # 途中でのアプリ切断
        await asyncio.wait_for(reader.read(-1), 5)  # 待つのではなく終わる
        # 後続の接続は通常どおり完走する
        data = await self.fetch(port)
        self.assertEqual(data, PAYLOAD)
        await asyncio.sleep(0.2)
        self.assertLess(self.client.mux.data_bytes, dualproxy.QUEUE_BYTES)

    async def test_small_exchange_completes_without_waiting_for_data(self):
        port = await self.start_target(echo)
        reader, writer, code = await socks_connect(self.client.listen_port, port)
        self.assertEqual(code, 0)
        writer.write(b"ping")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 2), b"ping")
        writer.close()

    async def test_refused_target_reports_failure(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        _, writer, code = await socks_connect(self.client.listen_port, closed_port)
        self.assertEqual(code, dualproxy.SOCKS_FAILED)
        writer.close()

    async def test_client_disconnect_closes_target_side(self):
        closed = asyncio.Event()

        async def wait_close(reader, writer):
            try:
                await reader.read(-1)
            except OSError:
                pass
            closed.set()
            writer.close()

        port = await self.start_target(wait_close)
        _, writer, code = await socks_connect(self.client.listen_port, port)
        self.assertEqual(code, 0)
        writer.close()
        await asyncio.wait_for(closed.wait(), 5)

    async def test_wrong_secret_is_rejected(self):
        bad = dualproxy.Client("127.0.0.1", b"wrong-secret-0123456789", SOURCES[:1],
                               server_port=self.server.port, listen_port=0)
        await bad.start()
        await asyncio.sleep(0.2)
        self.assertEqual(len(bad.active), 0)
        self.assertTrue(bad.auth_failed)
        await bad.stop()

    async def test_udp_associate_is_refused(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.client.listen_port)
        writer.write(b"\x05\x01\x00")
        await reader.readexactly(2)
        writer.write(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        reply = await reader.readexactly(10)
        self.assertEqual(reply[1], dualproxy.SOCKS_BAD_COMMAND)
        writer.close()


if __name__ == "__main__":
    unittest.main()
