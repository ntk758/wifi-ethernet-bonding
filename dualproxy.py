"""有線とWi-Fiを束ねて自前サーバー経由で通信するSOCKS5プロキシ。

PC側 (client) はアプリからのTCP接続を受け、経路ごとに張ったリンクへ分割して流す。
サーバー側 (server) は順序を戻して本来の接続先へ中継する。
空いたリンクが次のフレームを取りに行くので、速い経路ほど多く運ぶ。
リンクが切れると未確認のフレームを残りのリンクで再送するので、片方が落ちても接続は続く。

トンネルは認証するが暗号化しない。信頼できるLAN内でのみ使うこと。
"""
import argparse
import asyncio
import collections
import hashlib
import hmac
import os
import socket
import struct
import sys
import time

HEADER = struct.Struct("!BIQI")  # 種別, ストリームID, 通し番号または値, 長さ
# 受信は最大 MAX_FRAME まで受け付ける (将来の大型フレームに互換)。
# 送信は MAX_PAYLOAD に抑える。古い側のサーバー (32KB 超を拒否する) と動くうちは 32KB のまま。
# 両 PC を同一バージョンに更新したら MAX_PAYLOAD を 128KB に上げると 1フレームあたりの overhead が減り速くなる。
MAX_FRAME = 128 * 1024   # 受信の上限 (wire)
MAX_PAYLOAD = 32 * 1024  # 送信の切り出しサイズ
# ウィンドウは経路の往復時間 × 帯域より大きく取る。既定の 256KB 固定では
# Wi-Fi (RTT ~12ms) が 160Mbps 程度で頭打ちになるため、自動調整に任せる。
STREAM_WINDOW = 8 * 1024 * 1024
WINDOW_UPDATE = 256 * 1024  # window advance granularity; fewer but larger updates
LINK_INFLIGHT = 8 * 1024 * 1024   # per-link unacked budget; must stay above RTT x rate
WRITE_BUFFER = 1024 * 1024
QUEUE_BYTES = 8 * 1024 * 1024  # キューの上限はフレーム数ではなくバイト
ACK_BYTES = 256 * 1024  # batch acknowledgements this coarsely (bytes or frames, first hit wins)
ACK_FRAMES = 4
WRITE_BATCH = 8  # drain() ではなくこのフレームごとに送路の詰まりを見る (drain の待ちを減らす)
TICK = 0.005
PING_INTERVAL = 1.0
LINK_TIMEOUT = 5.0
CONNECT_TIMEOUT = 15.0
SESSION_TIMEOUT = 120.0
STREAM_IDLE_TIMEOUT = 60.0
MAX_STREAMS = 4096
REDETECT = 15.0
AUTH_BACKOFF = 10.0
LINK_SURVIVE_OK = 1.0  # a link dying younger than this marks the path unstable (back off)
RECONNECT = 0.2  # 張れていたリンクが切れた場合は、すぐ張り直す (待ちを長くすると復旧が遅い)
LINK_WAIT = 2.0  # 一時的な経路断は待って許容し、長い outage は速く失敗させる
STALL_RETRY = 0.5  # この時間を超えて未確認のフレームは、他の経路でも送る
STALL_RESEND = 4  # 1回のチェックで複製する上限
CLOSED_MEMORY = 100_000
DEFAULT_PORT = 7443

OPEN, OPEN_OK, OPEN_FAIL, DATA, FIN, RST, WINDOW, ACK, PING = range(1, 10)
SEQUENCED = {OPEN, OPEN_OK, OPEN_FAIL, DATA, FIN}
UNTRACKED = {ACK, PING}

SOCKS_OK, SOCKS_FAILED, SOCKS_BAD_COMMAND, SOCKS_BAD_ADDRESS = 0, 1, 7, 8


class AuthError(Exception):
    """共有鍵が一致しない。リトライしても結果は変わらないので明示する。"""


def note(message):
    print(message, file=sys.stderr, flush=True)


def encode(frame):
    kind, stream_id, value, payload = frame
    return HEADER.pack(kind, stream_id, value, len(payload)) + payload


def tune_socket(writer):
    sock = writer.get_extra_info("socket")
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    # SO_SNDBUF を固定すると Windows の自動調整が止まり、往復時間で帯域が決まる。
    # ここでは自動調整に任せ、asyncio 側のバッファだけ大きめにしておく。
    writer.transport.set_write_buffer_limits(high=WRITE_BUFFER)


class Stream:
    def __init__(self, mux, stream_id):
        self.mux = mux
        self.id = stream_id
        self.tx_seq = 0
        self.rx_next = 0
        self.rx_buffer = {}
        self.out = asyncio.Queue()
        self.sent = 0
        self.peer_consumed = 0
        self.window_open = asyncio.Event()
        self.window_open.set()
        self.consumed = 0
        self.announced = 0
        self.opened = asyncio.get_running_loop().create_future()
        self.run_task = None
        self.last_activity = time.monotonic()

    def touch(self):
        self.last_activity = time.monotonic()

    def next_seq(self):
        seq = self.tx_seq
        self.tx_seq += 1
        return seq

    def send_control(self, kind, payload=b""):
        self.mux.send_control((kind, self.id, self.next_seq(), payload))

    def on_frame(self, kind, seq, payload):
        if seq < self.rx_next or seq in self.rx_buffer:
            return  # 再送による重複
        self.rx_buffer[seq] = (kind, payload)
        while self.rx_next in self.rx_buffer:
            kind, payload = self.rx_buffer.pop(self.rx_next)
            self.rx_next += 1
            self.handle(kind, payload)

    def handle(self, kind, payload):
        self.touch()
        if kind == OPEN:
            self.run_task = asyncio.create_task(self.serve(payload))
        elif kind in (OPEN_OK, OPEN_FAIL):
            if not self.opened.done():
                self.opened.set_result(kind == OPEN_OK)
        elif kind == DATA:
            self.out.put_nowait(payload)
        elif kind == FIN:
            self.out.put_nowait(None)

    def on_window(self, consumed):
        self.touch()
        self.peer_consumed = max(self.peer_consumed, consumed)
        if self.sent - self.peer_consumed < STREAM_WINDOW:
            self.window_open.set()

    def abort(self):
        self.mux.forget(self)
        if self.run_task and self.run_task is not asyncio.current_task():
            self.run_task.cancel()

    async def serve(self, target):
        """サーバー側: 本来の接続先へつないで中継する。"""
        host, _, port = target.decode().rpartition(":")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host.strip("[]"), int(port)), CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError, ValueError):
            self.send_control(OPEN_FAIL)
            self.mux.forget(self)
            return
        tune_socket(writer)
        self.send_control(OPEN_OK)
        await self.relay(reader, writer)

    async def relay(self, reader, writer):
        try:
            await asyncio.gather(self.pump_out(reader), self.pump_in(writer))
        except (OSError, EOFError):
            self.mux.send_control((RST, self.id, 0, b""))
        except asyncio.CancelledError:
            # 中断された相手には「切れた」ことを伝える (黙って閉じると向こうが待ち続ける)
            self.mux.send_control((RST, self.id, 0, b""))
            raise
        finally:
            self.mux.forget(self)
            writer.close()

    async def pump_out(self, reader):
        while data := await reader.read(MAX_PAYLOAD):
            await self.window_open.wait()
            self.touch()
            self.sent += len(data)
            if self.sent - self.peer_consumed >= STREAM_WINDOW:
                self.window_open.clear()
            await self.mux.send_data((DATA, self.id, self.next_seq(), data))
        self.send_control(FIN)

    async def pump_in(self, writer):
        while (data := await self.out.get()) is not None:
            self.touch()
            writer.write(data)
            await writer.drain()
            self.consumed += len(data)
            if self.consumed - self.announced >= WINDOW_UPDATE:
                self.announced = self.consumed
                self.mux.send_control((WINDOW, self.id, self.consumed, b""))
        if writer.can_write_eof():
            writer.write_eof()


class Mux:
    """1セッション分のストリームとリンクを束ねる。"""

    def __init__(self, accept_streams):
        self.accept_streams = accept_streams
        self.streams = {}
        self.closed = collections.OrderedDict()
        self.links = set()
        self.retry = collections.deque()
        self.control = collections.deque()
        self.data = collections.deque()
        # 饥饿なリンクを順に回してフレームを渡す。全員を起こすと
        # 速いリンクがキューを空になるまで取って他の経路が飢える。
        self.demand = collections.deque()
        self.room_waiters = []
        self.next_stream_id = 1
        self.disconnected_at = time.monotonic()
        self.data_bytes = 0
        # 世代番号: サーバーが再起動した後の古いリンクの未確認フレームは
        # 新しいサーバーに流さない (幽霊のストリームを作るだけになる)。
        self.generation = 0

    def wake(self):
        self.distribute()

    def send_control(self, frame):
        self.control.append(frame)
        self.distribute()

    async def send_data(self, frame):
        while self.data_bytes >= QUEUE_BYTES:
            fut = asyncio.get_running_loop().create_future()
            self.room_waiters.append(fut)
            await fut
        self.data.append(frame)
        self.data_bytes += len(frame[3])
        self.distribute()

    def has_frames(self):
        return bool(self.retry or self.control or self.data)

    def take_frame(self):
        for queue in (self.retry, self.control, self.data):
            if queue:
                frame = queue.popleft()
                if queue is self.data:
                    self.data_bytes -= len(frame[3])
                    self.release_room()
                return frame
        return None

    def distribute(self):
        """フレームを、空きのある饥饿リンクへ順に配る。遅い経路はいっぱいで配られないので
        速い経路が自然に多く運ぶ。配れないリンクは最後尾に回す。"""
        rounds = len(self.demand)
        while rounds and self.demand and self.has_frames():
            link, fut = self.demand.popleft()
            rounds -= 1
            if fut.done():
                continue
            if link.inflight_bytes >= LINK_INFLIGHT:
                self.demand.append((link, fut))
                continue
            frame = self.take_frame()
            if frame is None:
                self.demand.append((link, fut))
                continue
            fut.set_result(frame)

    async def next_frame(self, link):
        fut = asyncio.get_running_loop().create_future()
        self.demand.append((link, fut))
        self.distribute()
        try:
            return await fut
        except asyncio.CancelledError:
            # The frame was already popped from the queue but never written. If we lose it
            # here its sequence number is gone forever and the stream stalls unrecoverably.
            if fut.done() and not fut.cancelled():
                self.retry.appendleft(fut.result())
                self.distribute()
            try:
                self.demand.remove((link, fut))
            except ValueError:
                pass
            raise

    def forget_waiting(self, link):
        restored = False
        for entry in list(self.demand):
            if entry[0] is link:
                self.demand.remove(entry)
                if entry[1].done() and not entry[1].cancelled():
                    # frame was handed to this link but the link died before writing it; resend
                    self.retry.appendleft(entry[1].result())
                    restored = True
                elif not entry[1].done():
                    entry[1].cancel()
        if restored:
            self.wake()

    def release_room(self):
        while self.room_waiters and self.data_bytes < QUEUE_BYTES:
            fut = self.room_waiters.pop(0)
            if not fut.done():
                fut.set_result(None)

    def receive(self, frame):
        kind, stream_id, value, payload = frame
        stream = self.streams.get(stream_id)
        if stream is None:
            if not self.accept_streams or kind not in SEQUENCED or stream_id in self.closed:
                return
            if len(self.streams) >= MAX_STREAMS:
                note(f"ストリームが多すぎます ({len(self.streams)})。新しい接続は破棄します")
                return
            stream = self.streams[stream_id] = Stream(self, stream_id)
        if kind == RST:
            stream.abort()
        elif kind == WINDOW:
            stream.on_window(value)
        else:
            stream.on_frame(kind, value, payload)

    def new_stream(self):
        stream = Stream(self, self.next_stream_id)
        self.streams[stream.id] = stream
        self.next_stream_id += 1
        return stream

    def forget(self, stream):
        if self.streams.pop(stream.id, None) is None:
            return
        self.closed[stream.id] = None
        if len(self.closed) > CLOSED_MEMORY:
            self.closed.popitem(last=False)
        # 队列に残ったフレームは次に張ったリンクで送られてしまうので、ここで捨てる
        for queue in (self.retry, self.control, self.data):
            for frame in list(queue):
                if frame[1] == stream.id:
                    queue.remove(frame)
                    if queue is self.data:
                        self.data_bytes -= len(frame[3])
        self.release_room()

    def add_link(self, link):
        self.links.add(link)

    def remove_link(self, link):
        self.links.discard(link)
        self.forget_waiting(link)
        if link.generation == self.generation:
            self.retry.extend(frame for frame, _ts in link.inflight)
        link.inflight.clear()
        if not self.links:
            self.disconnected_at = time.monotonic()
        self.wake()

    def reset(self):
        self.generation += 1  # これ以降に張られたリンクだけが新しいサーバーと組む
        for stream in list(self.streams.values()):
            stream.abort()
        for queue in (self.retry, self.control, self.data):
            queue.clear()
        self.data_bytes = 0
        for entry in list(self.demand):
            self.demand.remove(entry)
            if not entry[1].done():
                entry[1].cancel()
        self.release_room()


class Link:
    def __init__(self, mux, reader, writer, label):
        self.mux = mux
        self.reader = reader
        self.writer = writer
        self.label = label
        self.generation = mux.generation
        self.inflight = collections.deque()
        self.inflight_bytes = 0
        self.duplicated = set()
        self.pending_acks = 0
        self.pending_bytes = 0
        self.last_rx = time.monotonic()
        self.rx_bytes = 0
        self.tx_bytes = 0

    def write_raw(self, frame):
        """ACK と PING はキューを通さない。送路が詰まっていても生きたことを伝えられる。"""
        try:
            self.writer.write(encode(frame))
        except (OSError, RuntimeError):
            pass

    async def run(self):
        tune_socket(self.writer)
        self.mux.add_link(self)
        tasks = [asyncio.create_task(c) for c in (self.send_loop(), self.recv_loop(), self.heartbeat())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                error = None if task.cancelled() else task.exception()
                if task is tasks[2] and error is None:
                    note(f"リンク {self.label}: {LINK_TIMEOUT:g}秒応答がないので切ります")
                elif error is not None and not isinstance(error, (OSError, EOFError)):
                    note(f"リンク {self.label} が想定外のエラーで切れました: {error!r}")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.writer.transport.abort()
            self.mux.remove_link(self)

    async def send_loop(self):
        pack = HEADER.pack
        write = self.writer.write
        since_drain = 0
        while True:
            frame = await self.mux.next_frame(self)
            if frame[0] not in UNTRACKED:
                self.inflight.append((frame, time.monotonic()))
                self.inflight_bytes += len(frame[3])
            # header と payload を分けて書く: transport はバッファの要素を後でつなぐので、
            # 結合した bytes を作るときのような 1 フレームぶんのコピーが消える。
            write(pack(frame[0], frame[1], frame[2], len(frame[3])))
            if frame[3]:
                write(frame[3])
            self.tx_bytes += len(frame[3])
            # 送り張りの量は LINK_INFLIGHT で既に有界。drain は間隔を置いて確認する
            # (フレームごとに待つとイベントループの動きが増える)。
            since_drain += 1
            if since_drain >= WRITE_BATCH or self.inflight_bytes >= LINK_INFLIGHT:
                await self.writer.drain()
                since_drain = 0

    async def recv_loop(self):
        while True:
            kind, stream_id, value, length = HEADER.unpack(await self.reader.readexactly(HEADER.size))
            if length > MAX_FRAME:
                raise ValueError("フレームが大きすぎます")
            payload = await self.reader.readexactly(length) if length else b""
            self.last_rx = time.monotonic()
            if kind == ACK:
                for _ in range(min(value, len(self.inflight))):
                    frame, _ts = self.inflight.popleft()
                    self.inflight_bytes -= len(frame[3])
                self.mux.wake()
            elif kind != PING:
                self.rx_bytes += length
                self.pending_acks += 1
                self.pending_bytes += length
                self.mux.receive((kind, stream_id, value, payload))
                if self.pending_acks >= ACK_FRAMES or self.pending_bytes >= ACK_BYTES:
                    self.flush_acks()

    def flush_acks(self):
        if self.pending_acks:
            self.write_raw((ACK, 0, self.pending_acks, b""))
            self.pending_acks = 0
            self.pending_bytes = 0

    def resend_stalled(self, now):
        """遅い経路に捕まったフレームを、他の経路でもう一度送る。
        受信側は通し番号で重複を捨てるので、順序の待ち (head-of-line) を消せる。"""
        resent = 0
        for frame, sent_at in self.inflight:
            if resent >= STALL_RESEND:
                break
            if now - sent_at < STALL_RETRY:
                # inflight は送信順 (古い順) なので、これより後はまだ若い。走査を切る。
                break
            key = (frame[0], frame[1], frame[2])
            if key in self.duplicated:
                continue
            self.duplicated.add(key)
            self.mux.retry.appendleft(frame)
            resent += 1
        if resent:
            self.mux.distribute()
        if len(self.duplicated) > 4096:
            self.duplicated.clear()  # 記憶は大きくしない (複製は1回だけという目的に十分)

    async def heartbeat(self):
        last_ping = time.monotonic()
        while True:
            await asyncio.sleep(TICK)
            self.flush_acks()
            now = time.monotonic()
            if now - last_ping >= PING_INTERVAL:
                last_ping = now
                self.write_raw((PING, 0, 0, b""))
            self.resend_stalled(now)
            if now - self.last_rx > LINK_TIMEOUT:
                return


class Server:
    def __init__(self, secret, host="0.0.0.0", port=DEFAULT_PORT):
        self.secret = secret
        self.host = host
        self.port = port
        self.epoch = os.urandom(16)
        self.sessions = {}
        self.tasks = set()
        self.server = None

    async def start(self):
        try:
            self.server = await asyncio.start_server(self.handle, self.host, self.port)
        except OSError as e:
            sys.exit(f"{self.host}:{self.port} を開けません ({e})。他のプロセスが使っていないか確認してください")
        self.port = self.server.sockets[0].getsockname()[1]
        self.tasks.add(asyncio.create_task(self.reap()))

    async def reap(self):
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            for session_id, mux in list(self.sessions.items()):
                # 届かないフレームを待っているだけのストリームを放す (リンクがあっても)
                for stream in list(mux.streams.values()):
                    if stream.rx_buffer and now - stream.last_activity > STREAM_IDLE_TIMEOUT:
                        note(f"ストリーム{stream.id} を放棄 (待っているフレームが届きません)")
                        stream.abort()
                if not mux.links and now - mux.disconnected_at > SESSION_TIMEOUT:
                    mux.reset()
                    del self.sessions[session_id]

    async def handle(self, reader, writer):
        self.tasks.add(asyncio.current_task())
        try:
            nonce = os.urandom(32)
            writer.write(nonce)
            await writer.drain()
            reply = await asyncio.wait_for(reader.readexactly(48), LINK_TIMEOUT)
            expected = hmac.new(self.secret, nonce, hashlib.sha256).digest()
            if not hmac.compare_digest(reply[:32], expected):
                return
            writer.write(self.epoch)
            await writer.drain()
            mux = self.sessions.setdefault(reply[32:], Mux(accept_streams=True))
            await Link(mux, reader, writer, str(writer.get_extra_info("peername")[0])).run()
        except (OSError, EOFError, asyncio.TimeoutError):
            pass
        finally:
            self.tasks.discard(asyncio.current_task())
            writer.close()

    async def stop(self):
        self.server.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for mux in self.sessions.values():
            mux.reset()


class Client:
    def __init__(self, server_host, secret, sources, links_per_source=2,
                 server_port=DEFAULT_PORT, listen_host="127.0.0.1", listen_port=1080,
                 auto_sources=False):
        self.server_host = server_host
        self.server_port = server_port
        self.secret = secret
        self.sources = sources
        self.links_per_source = links_per_source
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.session_id = os.urandom(16)
        self.epoch = None
        self.mux = Mux(accept_streams=False)
        self.active = set()
        self.tasks = []
        self.handlers = set()
        self.server = None
        self.auth_failed = False
        self.auth_noted = False
        # 経路は起動時に焼き込まず、必要なら自分で見つける (新しい経路はリンクを追加するだけ)
        self.auto_sources = auto_sources
        self.source_tasks = {}
        self.unusable = dict.fromkeys(sources, 0)

    async def start(self):
        try:
            self.server = await asyncio.start_server(self.handle_socks, self.listen_host, self.listen_port)
        except OSError as e:
            sys.exit(f"{self.listen_host}:{self.listen_port} を開けません ({e})。他のプロキシが使っていないか確認してください")
        self.listen_port = self.server.sockets[0].getsockname()[1]
        for source in list(self.sources):
            self.ensure_source(source)
        if self.auto_sources:
            self.tasks.append(asyncio.create_task(self.manage_sources()))

    def ensure_source(self, source):
        if source in self.source_tasks:
            return
        # 自動検出の経路は __init__ の後で来る。数え始めておかないと、最初の失敗の
        # 処理で KeyError になり、その経路のタスクが黙って消える。
        self.unusable.setdefault(source, 0)
        tasks = [asyncio.create_task(self.maintain_link(source)) for _ in range(self.links_per_source)]
        self.source_tasks[source] = tasks
        self.tasks.extend(tasks)

    def drop_source(self, source):
        """その経路を諦める。張っていたリンクは切られ、未確認のフレームは他の経路で再送される。"""
        for task in self.source_tasks.pop(source, []):
            task.cancel()

    def detect_sources(self):
        try:
            import dualdl
        except Exception:
            return []
        try:
            return dualdl.detect_source_ips() or []
        except Exception:
            return []

    async def manage_sources(self):
        while True:
            await asyncio.sleep(REDETECT)
            found = await asyncio.to_thread(self.detect_sources)
            if not found:
                continue  # 一時的に全部のアダプタが見えなくなっても、既存の経路は維持する
            for source in found:
                if source not in self.source_tasks:
                    note(f"経路を追加: {source}")
                    self.ensure_source(source)
            for source in list(self.source_tasks):
                if source not in found:
                    note(f"経路を削除: {source}")
                    self.drop_source(source)

    async def maintain_link(self, source):
        while True:
            try:
                survived = await self.connect_link(source)
                # 張れていたリンクが切れて戻ってきた: すぐ張り直す (復旧を待たせない)
                if survived < LINK_SURVIVE_OK:
                    # link died right after connecting: handshake works but no data moves.
                    # Fast reconnect churn would flood the retry queue; back off like a downed path.
                    self.unusable[source] += 1
                    if self.unusable[source] == 5:
                        note(f"path {source} link keeps dying right after reconnect; backoff raised")
                    await asyncio.sleep(1.0 if self.unusable[source] < 5 else 5.0)
                    continue
                self.unusable[source] = 0
                await asyncio.sleep(RECONNECT)
                continue
            except AuthError:
                self.auth_failed = True
                if not self.auth_noted:
                    self.auth_noted = True
                    note("共有鍵が一致しません。dualproxy.key か DUALPROXY_SECRET をサーバーと同じにしてしてください")
                await asyncio.sleep(AUTH_BACKOFF)
                continue
            except (OSError, EOFError, asyncio.TimeoutError) as e:
                self.unusable[source] += 1
                if self.unusable[source] == 5:
                    note(f"経路 {source} が張れません (サーバー到達不可/アダプタ低下): {e!r}")
                # 数回の失敗は 1 秒間隔で続けます (一時的な切断の復旧を遅くしない)。
                # 何度も張れない経路は間隔を広げて叩き続けます。
                await asyncio.sleep(1.0 if self.unusable[source] < 5 else 5.0)
            except Exception as e:
                # ここで抜けるとこの経路は二度と張り直されず、黙って片方の経路だけになる。
                # タスクは self.tasks が持っているので、例外はどこにも表示されない。
                note(f"経路 {source} のリンクで想定外のエラー: {e!r} (張り直します)")
                await asyncio.sleep(1.0)

    async def connect_link(self, source):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.server_host, self.server_port, local_addr=(source, 0)),
            LINK_TIMEOUT,
        )
        try:
            nonce = await asyncio.wait_for(reader.readexactly(32), LINK_TIMEOUT)
            writer.write(hmac.new(self.secret, nonce, hashlib.sha256).digest() + self.session_id)
            await writer.drain()
            try:
                epoch = await asyncio.wait_for(reader.readexactly(16), LINK_TIMEOUT)
            except EOFError:
                raise AuthError("サーバーが共有鍵を拒否しました")
            if self.epoch not in (None, epoch):
                self.mux.reset()  # サーバーが再起動したので既存の接続は続けられない
            self.epoch = epoch
            link = Link(self.mux, reader, writer, source)
            self.active.add(link)
            started = time.monotonic()
            try:
                await link.run()
            finally:
                self.active.discard(link)
        finally:
            writer.close()
        return time.monotonic() - started

    async def wait_for_link(self, timeout):
        """経路が張れるまで待つ。一時的な切断は救い、長い outage は速く諦める。"""
        deadline = time.monotonic() + timeout
        while not self.mux.links and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return bool(self.mux.links)

    async def handle_socks(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            target = await self.socks_handshake(reader, writer)
            if target is None:
                return
            if not await self.wait_for_link(LINK_WAIT):
                # 経路が無いときに 20 秒も待たせない。アプリはすぐ別の手段を試せる。
                await self.socks_reply(writer, SOCKS_FAILED)
                return
            tune_socket(writer)
            stream = self.mux.new_stream()
            stream.run_task = task
            stream.send_control(OPEN, target.encode())
            try:
                ok = await asyncio.wait_for(stream.opened, CONNECT_TIMEOUT + LINK_TIMEOUT)
            except asyncio.TimeoutError:
                self.mux.forget(stream)  # 開けなかった接続を队列に残すと幽霊の接続先が開く
                await self.socks_reply(writer, SOCKS_FAILED)
                return
            await self.socks_reply(writer, SOCKS_OK if ok else SOCKS_FAILED)
            if not ok:
                self.mux.forget(stream)
                return
            await stream.relay(reader, writer)
        except asyncio.TimeoutError:
            await self.socks_reply(writer, SOCKS_FAILED)
        except (OSError, EOFError, ValueError):
            pass
        finally:
            self.handlers.discard(task)
            writer.close()

    async def socks_handshake(self, reader, writer):
        version, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        if version != 5 or 0 not in methods:
            writer.write(b"\x05\xff")
            return None
        writer.write(b"\x05\x00")
        await writer.drain()
        _, command, _, address_type = await reader.readexactly(4)
        if address_type == 1:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif address_type == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        elif address_type == 4:
            host = "[" + socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16)) + "]"
        else:
            await self.socks_reply(writer, SOCKS_BAD_ADDRESS)
            return None
        port = int.from_bytes(await reader.readexactly(2), "big")
        if command != 1:  # UDPは扱わない
            await self.socks_reply(writer, SOCKS_BAD_COMMAND)
            return None
        return f"{host}:{port}"

    async def socks_reply(self, writer, code):
        writer.write(bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
        await writer.drain()

    def received_by_source(self):
        totals = {}
        for link in self.active:
            totals[link.label] = totals.get(link.label, 0) + link.rx_bytes + link.tx_bytes
        return totals

    async def report(self):
        previous = {}
        while True:
            await asyncio.sleep(1)
            totals = self.received_by_source()
            rates = [f"{ip} {max(n - previous.get(ip, 0), 0) * 8 / 1e6:.0f}Mbps" for ip, n in totals.items()]
            previous = totals
            status = "認証失敗" if self.auth_failed else f"リンク{len(self.active)}本"
            try:
                print(f"\r{status}  " + "  ".join(rates) + "   ", end="", flush=True)
            except (OSError, ValueError):
                # 出力先がない (隠れた窓から起動した) でも、接続は続ける
                pass

    async def stop(self):
        self.server.close()
        tasks = self.tasks + list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.mux.reset()


def read_secret(path):
    secret = os.environ.get("DUALPROXY_SECRET", "").encode()
    if path:
        with open(path, "rb") as f:
            secret = f.read().strip()
    if len(secret) < 16:
        sys.exit("16文字以上の共有鍵を --secret-file か環境変数 DUALPROXY_SECRET で指定してください")
    return secret


async def serve_forever(args):
    server = Server(read_secret(args.secret_file), args.listen, args.port)
    await server.start()
    print(f"待ち受け: {server.host}:{server.port}")
    await asyncio.Event().wait()


async def client_forever(args):
    auto = not args.bind
    client = Client(args.server, read_secret(args.secret_file), list(args.bind or []), args.links,
                    args.port, listen_port=args.socks_port, auto_sources=auto)
    if auto:
        # 起動時に経路が揃っていなくても、待ち受けは開いておき、見えた張る
        client.sources = client.detect_sources()
    elif not client.sources:
        sys.exit("使えるネットワーク経路がありません (--bind に指定したアドレスが使えません)")
    await client.start()
    listing = ", ".join(client.sources) if client.sources else "(まだ経路なし: 自動検出を継続)"
    print(f"経路: {listing}  SOCKS5: 127.0.0.1:{client.listen_port}")
    await client.report()


def main():
    parser = argparse.ArgumentParser(description="有線とWi-Fiを束ねるSOCKS5プロキシ")
    sub = parser.add_subparsers(dest="mode", required=True)
    server = sub.add_parser("server", help="サーバー側で動かす")
    server.add_argument("--listen", default="0.0.0.0")
    server.add_argument("--port", type=int, default=DEFAULT_PORT)
    server.add_argument("--secret-file")
    client = sub.add_parser("client", help="PC側で動かす")
    client.add_argument("server", help="サーバーのIPアドレス")
    client.add_argument("--port", type=int, default=DEFAULT_PORT)
    client.add_argument("--bind", nargs="+", metavar="IP",
                       help="使う送信元IP (省略時は自動検出し、増減を反映する)")
    client.add_argument("--links", type=int, default=2, help="経路ごとのリンク数 (既定: 2)")
    client.add_argument("--socks-port", type=int, default=1080)
    client.add_argument("--secret-file")
    args = parser.parse_args()
    try:
        asyncio.run(serve_forever(args) if args.mode == "server" else client_forever(args))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        sys.exit(f"起動できません: {e}")


if __name__ == "__main__":
    main()
