"""サーバーPC用のファイルを、LAN内へ一時的に配る。

決めたファイルだけを配り (共有鍵は配らない)、LAN外からの接続は拒否し、一定時間で止まる。
起動するとサーバーPCで貼り付ける取得コマンドを表示する。コマンドには各ファイルの
SHA256が入っていて、一致しないファイルは置き換えない。
使い方: python share_server_files.py [--minutes 15] [--port 8765]
"""
import argparse
import hashlib
import http.server
import ipaddress
import os
import socket
import threading

FILES = ["dualproxy.py", "start_server.ps1", "install_server.ps1", "SETUP.md"]
LAN = ipaddress.ip_network("192.168.3.0/24")
SERVER_DIR = r"C:\Wifi and Ethernet"


def lan_address():
    """LAN側に出るときの自分のIPv4を返す (実際には送信しない)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.168.3.1", 80))
        return s.getsockname()[0]


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest().upper()


def download_command(base, hashes):
    entries = "; ".join(f"'{name}'='{digest}'" for name, digest in hashes.items())
    return "\n".join([
        f"$base = '{base}'",
        f"$dir = '{SERVER_DIR}'",
        f"$files = [ordered]@{{ {entries} }}",
        "New-Item -ItemType Directory -Force $dir | Out-Null",
        "foreach ($name in $files.Keys) {",
        "    $tmp = Join-Path $dir \"$name.download\"",
        "    Invoke-WebRequest \"$base/$name\" -OutFile $tmp -UseBasicParsing",
        "    if ((Get-FileHash $tmp -Algorithm SHA256).Hash -ne $files[$name]) {",
        "        Remove-Item $tmp; throw \"SHA256 mismatch: $name (not replaced)\"",
        "    }",
        "    $dest = Join-Path $dir $name",
        "    if (Test-Path $dest) { Copy-Item $dest \"$dest.bak\" -Force }",
        "    Move-Item $tmp $dest -Force",
        "    \"updated: $name\"",
        "}",
    ])


class Handler(http.server.BaseHTTPRequestHandler):
    hashes = {}

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {fmt % args}", flush=True)

    def do_GET(self):
        if ipaddress.ip_address(self.client_address[0]) not in LAN:
            self.send_error(403)
            return
        name = self.path.lstrip("/")
        if name not in self.hashes:
            self.send_error(404)
            return
        with open(name, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="サーバーPC用のファイルをLAN内へ一時的に配る")
    parser.add_argument("--minutes", type=float, default=15, help="この時間で自動停止 (既定: 15分)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    Handler.hashes = {name: sha256(name) for name in FILES}
    address = lan_address()
    httpd = http.server.ThreadingHTTPServer((address, args.port), Handler)
    threading.Timer(args.minutes * 60, httpd.shutdown).start()

    print(f"配布中: http://{address}:{args.port}/ ({args.minutes:g}分で自動停止、Ctrl+Cで中止)")
    print("サーバーPCのPowerShellに、次をまとめて貼り付けてください:\n")
    print(download_command(f"http://{address}:{args.port}", Handler.hashes))
    print(flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        os._exit(0)  # 残ったタイマーを待たずに終わる


if __name__ == "__main__":
    main()
