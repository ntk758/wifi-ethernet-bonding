"""経路の健全性チェック (SETUP.md §4 用)。

各インターフェースの IPv4 から、ゲートウェイとサーバーへ TCP が張れるかを順に試す。
「Wi-Fi は繋がっているのに link が張れない」ときに、どの経路のどこまで届かないかを一行ずつ示す。
使い方: python check_paths.py [サーバーIP] [ポート]  (既定 192.168.3.22 7443)
"""
import socket
import subprocess
import sys
import time

SERVER_DEFAULT = ("192.168.3.22", 7443)
GATEWAY_DEFAULT = "192.168.3.1"
TIMEOUT = 3.0


def interfaces():
    """(状態, IPv4, インデックス) を返す (169.254.x と 127.x は除く)。
    PowerShell の名前は日本語になるので、出力は ASCII だけ通す (cp932 で壊れない)。"""
    script = "Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { " \
        "$a = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue; " \
        "'{0} {1} {2}' -f $_.IPAddress, $_.InterfaceIndex, $a.Status }"
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=30).stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"インターフェース一覧が取れません ({e!r})。--bind 相当の確認は PowerShell で直接どうぞ")
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ip, idx, status = parts[0], parts[1], (parts[2] if len(parts) > 2 else "?")
        if ip.startswith(("169.254.", "127.")):
            continue
        found.append((status or "?", ip, idx))
    return found


def rtt(source_ip, target):
    s = socket.socket()
    s.settimeout(TIMEOUT)
    try:
        s.bind((source_ip, 0))
        t0 = time.monotonic()
        s.connect(target)
        return f"OK {(time.monotonic() - t0) * 1000:.1f}ms"
    except OSError as e:
        return f"FAIL {type(e).__name__} {e}"
    finally:
        s.close()


def main():
    server_ip = sys.argv[1] if len(sys.argv) > 1 else SERVER_DEFAULT[0]
    server_port = int(sys.argv[2]) if len(sys.argv) > 2 else SERVER_DEFAULT[1]
    gateway = sys.argv[3] if len(sys.argv) > 3 else GATEWAY_DEFAULT
    rows = interfaces()
    if not rows:
        print("使える IPv がありません (アダプタが見えないか、全部 link-down)")
        return
    for status, ip, idx in rows:
        gw = rtt(ip, (gateway, 80))
        srv = rtt(ip, (server_ip, server_port))
        print(f"{ip:16s} (if#{idx} adapter={status}): gateway {gw} | server {server_ip}:{server_port} {srv}")
        if srv.startswith("OK") and gw.startswith("OK"):
            print("    -> OK: この経路は使えます (束ねに参加します)")
        elif gw.startswith("FAIL"):
            print("    -> ゲートウェイに届きません: AP/ルーターの上りが壊れています "
                  "(DHCP は通っても通信はできません)。AP の LAN 接続/分離設定を確認")


if __name__ == "__main__":
    main()
