"""有線とWi-Fiを同時に使ってHTTP(S)ファイルをダウンロードする。

ファイルを小さな範囲に分け、各経路のワーカーが空き次第次の範囲を取りに行く。
速い経路ほど多くの範囲を運ぶので、経路の速度差を気にせず合算できる。
"""
import argparse
import http.client
import os
import queue
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse

CHUNK_SIZE = 8 * 1024 * 1024
READ_SIZE = 1024 * 1024  # 1回の読み込み。小さいと Python のループとロックの回数が増える
STATS_STEP = 4 * 1024 * 1024  # 統計の更新間隔 (共有ロックをこのごとにしか掴まない)
TIMEOUT = 30
MAX_REDIRECTS = 5
MAX_CONSECUTIVE_FAILURES = 3
MAX_CHUNK_ATTEMPTS = 3
USER_AGENT = "dualdl/0.1"


class DownloadError(Exception):
    pass


def detect_source_ips():
    """接続中でデフォルトゲートウェイを持つインターフェースのIPv4アドレスを返す。

    切断直後のアダプターにも経路とIPが残るため、リンク状態で絞り込む。
    """
    script = (
        "Get-NetAdapter | Where-Object Status -eq 'Up' | Where-Object { "
        "Get-NetRoute -InterfaceIndex $_.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue } | "
        "ForEach-Object { (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4).IPAddress }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except subprocess.TimeoutExpired:
        print("注意: 経路の検出がタイムアウトしました (--bind で明示してください)", file=sys.stderr)
        return []
    ips = [line.strip() for line in out.splitlines() if line.strip()]
    return list(dict.fromkeys(ip for ip in ips if not ip.startswith("169.254.")))


def open_connection(url, source_ip):
    if url.scheme == "https":
        return http.client.HTTPSConnection(
            url.hostname, url.port, timeout=TIMEOUT, source_address=(source_ip, 0),
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(
        url.hostname, url.port, timeout=TIMEOUT, source_address=(source_ip, 0),
    )


def request_target(url):
    return url.path + ("?" + url.query if url.query else "") or "/"


def send_range(conn, url, start, end):
    conn.request("GET", request_target(url), headers={
        "Range": f"bytes={start}-{end}",
        "User-Agent": USER_AGENT,
    })
    return conn.getresponse()


def probe(raw_url, source_ips):
    """リダイレクトを解決し、最終URL・サイズ・ETagを返す。使えた経路を覚えておく。"""
    errors = []
    for source_ip in source_ips:
        url = urllib.parse.urlsplit(raw_url)
        try:
            for _ in range(MAX_REDIRECTS + 1):
                conn = open_connection(url, source_ip)
                try:
                    resp = send_range(conn, url, 0, 0)
                    resp.read(1024)
                    if resp.status in (301, 302, 303, 307, 308):
                        raw_url = urllib.parse.urljoin(raw_url, resp.getheader("Location"))
                        url = urllib.parse.urlsplit(raw_url)
                        continue
                    if resp.status == 200:
                        raise DownloadError("サーバーが範囲指定に対応していないため分割できません")
                    if resp.status != 206:
                        raise DownloadError(f"HTTP {resp.status} が返りました")
                    total = resp.getheader("Content-Range", "").rpartition("/")[2]
                    if not total.isdigit():
                        raise DownloadError("ファイルサイズを取得できません")
                    return url, int(total), resp.getheader("ETag"), source_ip
                finally:
                    conn.close()
        except DownloadError as e:
            errors.append(f"{source_ip}: {e}")
        except OSError as e:
            # その経路が使えないだけ。別の経路で続けられるので落とさない。
            errors.append(f"{source_ip}: {e!r}")
    raise DownloadError("どの経路でも接続できません: " + "; ".join(errors))


class Download:
    def __init__(self, url, size, etag, part_path, source_ips, streams):
        self.url = url
        self.size = size
        self.etag = etag
        self.part_path = part_path
        self.source_ips = source_ips
        self.streams = streams
        self.chunks = queue.Queue()
        for start in range(0, size, CHUNK_SIZE):
            self.chunks.put((start, min(start + CHUNK_SIZE, size) - 1))
        self.remaining = self.chunks.qsize()
        self.received = dict.fromkeys(source_ips, 0)
        self.lock = threading.Lock()
        self.last_error = None
        # 範囲ごとの試行回数。ワーカーの連続失敗とは別に数える。
        self.attempts = {}
        self.give_up_chunks = set()

    def give_up(self, start, end):
        """この範囲を諦めたか (True なら再送しない)。"""
        with self.lock:
            if self.attempts.get((start, end), 0) >= MAX_CHUNK_ATTEMPTS:
                self.give_up_chunks.add((start, end))
                return True
            self.attempts[(start, end)] = self.attempts.get((start, end), 0) + 1
            return False

    def fetch_chunk(self, conn, out, source_ip, start, end):
        resp = send_range(conn, self.url, start, end)
        if resp.status != 206:
            resp.read()
            raise DownloadError(f"HTTP {resp.status}")
        # 範囲の検査は寛容に: クランプされたendや、範囲レスポンスにETagを載せない
        # サーバーがある (厳格に比較すると本来できるダウンロードが失敗していた)。
        content_range = resp.getheader("Content-Range", "")
        span, _, total = content_range.partition("/")
        got = re.fullmatch(r"bytes (\d+)-(\d+)", span.strip())
        if not got or int(got.group(1)) != start or int(got.group(2)) > end:
            raise DownloadError("要求と異なる範囲が返りました")
        if not total.isdigit() or int(total) != self.size:
            raise DownloadError("ファイルサイズが変化しました")
        etag = resp.getheader("ETag")
        if self.etag and etag and etag != self.etag:
            raise DownloadError("ダウンロード中にファイルが変更されました")
        out.seek(start)
        done = 0
        reported = 0
        try:
            while data := resp.read(READ_SIZE):
                out.write(data)
                done += len(data)
                if done - reported >= STATS_STEP:
                    # 共有ロックはまとめて掴む (小さな読み毎にロックすると競合する)
                    with self.lock:
                        self.received[source_ip] += done - reported
                    reported = done
            if done != int(got.group(2)) - start + 1:
                raise DownloadError("受信サイズが一致しません")
            if done > reported:
                with self.lock:
                    self.received[source_ip] += done - reported
        except Exception:
            with self.lock:
                self.received[source_ip] -= reported
            raise

    def worker(self, source_ip):
        conn = None
        failures = 0
        with open(self.part_path, "r+b") as out:
            while failures < MAX_CONSECUTIVE_FAILURES:
                try:
                    start, end = self.chunks.get_nowait()
                except queue.Empty:
                    break
                try:
                    conn = conn or open_connection(self.url, source_ip)
                    self.fetch_chunk(conn, out, source_ip, start, end)
                except (OSError, http.client.HTTPException, DownloadError) as e:
                    self.last_error = f"{source_ip}: {e!r}"
                    if self.give_up(start, end):
                        # この範囲は諦め、このワーカーの連続失敗は数えない
                        # (同じ範囲を3回失敗で全体が失敗していた)
                        pass
                    else:
                        self.chunks.put((start, end))
                        failures += 1
                    if conn:
                        conn.close()
                    conn = None
                    time.sleep(1)
                else:
                    failures = 0
                    with self.lock:
                        self.remaining -= 1
        if conn:
            conn.close()

    def report(self, previous):
        parts = []
        for ip in self.source_ips:
            parts.append(f"{ip} {(self.received[ip] - previous[ip]) * 8 / 1e6:.0f}Mbps")
        done = sum(self.received.values())
        percent = done / self.size if self.size else 1.0
        print(f"\r{percent:6.1%}  " + "  ".join(parts) + "   ", end="", flush=True)

    def run(self):
        threads = [
            threading.Thread(target=self.worker, args=(ip,), daemon=True)
            for ip in self.source_ips for _ in range(self.streams)
        ]
        started = time.time()
        for t in threads:
            t.start()
        previous = dict(self.received)
        while any(t.is_alive() for t in threads):
            time.sleep(1)
            self.report(previous)
            previous = dict(self.received)
        print()
        if self.give_up_chunks:
            raise DownloadError(
                f"{len(self.give_up_chunks)} 範囲がどの経路でも失敗しました ({self.last_error})")
        if self.remaining:
            raise DownloadError(f"全経路で失敗しました ({self.last_error})")
        return time.time() - started


def download(raw_url, output, source_ips, streams=4, force=False):
    if os.path.exists(output) and not force:
        raise DownloadError(f"{output} は既に存在します (上書きは --force)")
    url, size, etag, used_ip = probe(raw_url, source_ips)
    if size == 0:
        # 空のファイルは範囲を作れないので、そのまま書き出す (0で割らない)
        with open(output, "wb"):
            pass
        return 0, 0.0, dict.fromkeys(source_ips, 0)
    part_path = output + ".part"
    with open(part_path, "wb") as f:
        f.truncate(size)
    # probe が使えた経路を覚えているが、他の経路もそのまま参加させる
    # (途中で切れた経路の範囲は、生きている経路が拾う)
    dl = Download(url, size, etag, part_path, source_ips, streams)
    try:
        elapsed = dl.run()
    except BaseException:
        os.remove(part_path)
        raise
    os.replace(part_path, output)
    return size, elapsed, dl.received


def main():
    parser = argparse.ArgumentParser(description="有線とWi-Fiを同時に使ってダウンロードする")
    parser.add_argument("url")
    parser.add_argument("-o", "--output", help="保存先 (省略時はURLのファイル名)")
    parser.add_argument("--bind", nargs="+", metavar="IP", help="使う送信元IP (省略時は自動検出)")
    parser.add_argument("--streams", type=int, default=4, help="経路ごとの接続数 (既定: 4)")
    parser.add_argument("--force", action="store_true", help="既存ファイルを上書きする")
    args = parser.parse_args()

    output = args.output or os.path.basename(urllib.parse.urlsplit(args.url).path) or "download.bin"
    source_ips = args.bind or detect_source_ips()
    if not source_ips:
        sys.exit("使えるネットワーク経路がありません")
    if len(source_ips) < 2:
        print(f"注意: 経路が1つだけです ({source_ips[0]})。Wi-Fiが接続されているか確認してください", file=sys.stderr)
    print("経路: " + ", ".join(source_ips))

    try:
        size, elapsed, received = download(args.url, output, source_ips, args.streams, args.force)
    except DownloadError as e:
        sys.exit(f"エラー: {e}")
    except OSError as e:
        # 経路が一時的に消えた・IPが変わったなどの場合。tracebackではなく説明を出す。
        sys.exit(f"エラー: 経路が使えませんでした ({e!r})。--bind で使うIPを明示してください")
    if size == 0:
        print(f"完了: {output} (空のファイル)")
        return
    rate = size * 8 / elapsed / 1e6 if elapsed else 0
    print(f"完了: {output} ({size / 1e6:.0f} MB, {elapsed:.1f} 秒, 平均 {rate:.0f} Mbps)")
    for ip, n in received.items():
        print(f"  {ip}: {n / size:.0%}")


if __name__ == "__main__":
    main()
