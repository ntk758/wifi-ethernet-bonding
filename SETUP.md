# 有線 + Wi-Fi を束ねるプロキシ: セットアップ手順

有線と Wi-Fi の 2 経路を 1 本の SOCKS5 トンネルに束ねて、自宅サーバー (192.168.3.22) 経由で通信します。
PC のアプリは sing-box の TUN を経由して `127.0.0.1:1080` の SOCKS5 に流れ、クライアントが 2 経路に分割して
サーバー PC に送ります。サーバー PC は順序を戻して本来の接続先へ中継します。

```
アプリ → TUN (sing-box) → SOCKS5 127.0.0.1:1080 → dualproxy client ─┬─ 有線 ─┐
                                                                      └─ Wi-Fi ─┤→ dualproxy server → 接続先
```

※ トンネルは TCP のみ (IPv4 と IPv6)。UDP は物理アダプタを直接使います (束ね対象外)。
  HTTP/3 (UDP 443) は遮断して、アプリに TCP へ切り替えさせます (切り替えると束ねの対象になります)。
※ トンネルは暗号化しません。信頼できる LAN 内だけで使ってください。

配置する順序 (更新を配布するときも同じ):

1. サーバー PC に `dualproxy.py`・`dualproxy.key` をコピー → サーバーを起動 (§1)
2. PC (クライアント) に同じファイルを置く → クライアントと TUN を起動 (§2)

`dualproxy.py` は両 PC で**同じ内容**にします (鍵が違うと認証が落ちます)。
通信の途中でも、クライアントだけを新しい版に差し替えても動きます (旧サーバーとは 32KB フレームで
互換を保つ設計です。両側が新しい版になってから大きなフレームに上げられます → §3)。

---

## 0. 必要なもの (両 PC)

- Python 3.11 以上 (`python --version` で確認)
- 共通の鍵 `dualproxy.key` (16文字以上。両 PC で**同じ内容**が必要)
- このフォルダのファイル: `dualproxy.py`, `dualproxy.key`, `start_server.ps1`, `install_server.ps1`,
  `start_client.ps1`, `install_client.ps1`, `singbox\` (Windows の TUN 用)

鍵を作る (まだ無い場合):

```powershell
python -c "import os,pathlib;pathlib.Path('dualproxy.key').write_bytes(os.urandom(32))"
```

---

## 1. サーバー PC のセットアップ

サーバー PC は管理者権限が不要です。SYSTEM が読める場所 (例: `C:\dualproxy`) に置くとタスク起動が簡単です。

```powershell
# 1) ファイルを置く
New-Item -ItemType Directory -Force -Path C:\dualproxy | Out-Null
# 共有フォルダまたは USB から dualproxy.py / dualproxy.key / start_server.ps1 / install_server.ps1 をコピー
#   例: Copy-Item \\DESKTOP-MAIN\dualproxy\* C:\dualproxy\ -Force

# 2) 鍵が PC と同じか確認 (同じサイズ・同じ内容)
Get-FileHash C:\dualproxy\dualproxy.key

# 3) 手動で起動して動くことを確認
cd C:\dualproxy
python dualproxy.py server --listen 0.0.0.0 --port 7443 --secret-file dualproxy.key
# → "待ち受け: 0.0.0.0:7443" が出る

# 4) PC 側から届くか確認
#   (PC 側で実行) Test-NetConnection 192.168.3.22 -Port 7443   → True
```

再起動後も自動で起動するようにします (**管理者** PowerShell で):

```powershell
cd C:\dualproxy
.\install_server.ps1 -Listen 0.0.0.0 -Port 7443 -SecretFile dualproxy.key
# → タスク "dualproxy-server" が登録され、起動する

# 確認
Get-ScheduledTask -TaskName dualproxy-server | Select-Object TaskName, State
Get-NetTCPConnection -LocalPort 7443 -State Listen
Get-NetTCPConnection -RemotePort 7443 -State Established | Group-Object LocalAddress
```

サーバーのイベントは `C:\dualproxy\server.log` に書かれます。

---

## 2. PC (クライアント) のセットアップ

```powershell
cd D:\Wifi and Ethernet

# 1) 手動でクライアントを起動 (経路は自動検出する。--bind を書くと固定)
python dualproxy.py client 192.168.3.22 --secret-file dualproxy.key --socks-port 1080
# → "経路: 192.168.3.10, 192.168.3.36  SOCKS5: 127.0.0.1:1080" と、本数のレポートが流れる

# 2) 2 経路が張れているか確認 (両方の IP が現れる)
Get-NetTCPConnection -RemotePort 7443 -State Established | Group-Object LocalAddress

# 3) プロキシ経由の通信を確認
curl.exe --socks5-hostname 127.0.0.1:1080 -o NUL -w "%{http_code} %{speed_download}B/s`n" https://proof.ovh.net/files/100Mb.dat
```

TUN (システム全体の通信をプロキシに流す) を有効化 (**管理者** PowerShell で):

```powershell
cd D:\Wifi and Ethernet\singbox
.\install_tun.ps1          # タスク "dualproxy-tun" を登録 (SYSTEM, 起動時に実行)
# 確認
Get-NetAdapter -Name dualproxy-tun
Get-NetRoute -InterfaceIndex (Get-NetAdapter -Name dualproxy-tun).ifIndex | Select-Object DestinationPrefix
curl.exe -o NUL -w "%{http_code} %{time_total}s`n" https://example.com   # TUN 経由の確認
```

常時運転 (クラッシュしても再起動、経路の増減はクライアント側が反映) (**管理者** PowerShell で):

```powershell
cd D:\Wifi and Ethernet
.\install_client.ps1 -Server 192.168.3.22 -ServerPort 7443 -SocksPort 1080
# 確認
Get-ScheduledTask -TaskName dualproxy-client | Select-Object TaskName, State
Get-ScheduledTask -TaskName dualproxy-tun    | Select-Object TaskName, State
Get-Content client.log -Tail 20      # クライアントの起動/終了の記録
Get-Content singbox\tun-watch.log -Tail 20   # トンネルの健全性判定の記録
```

### やめる / 元に戻す

```powershell
Unregister-ScheduledTask -TaskName dualproxy-client -Confirm:$false
.\singbox\remove_tun.ps1        # TUN とタックを削除 (通信は直接経路に戻ります)
Unregister-ScheduledTask -TaskName dualproxy-server -Confirm:$false   # サーバー側
```

---

## 3. 速度の確認と調整

測測結果:

| 条件 | 結果 |
|---|---|
| 初版・有線 1 経路のみ | 33〜46 Mbps |
| 初版・両経路 (既定定数) | 114 Mbps (1流), 約152 Mbps (4並列) |
| 定数調整後・両経路 | 単流 115 Mbps, Wi-Fi 側が 92→150 Mbps に増加 |
| ループバックでプロトコル単体 (A/B, 2026-11-10 朝) | 4 並列: 旧定数 1004〜1062 Mbps → 新定数 1278〜1714 Mbps (+20〜70%) |
| ループバックでプロトコル単体 (A/B, 2026-11-10 深夜, 実装も优化) | 4 並列: 旧定数 1626〜1759 Mbps → 新定数 2221〜2420 Mbps。初版比較では 1004 → 2221〜2420 (=約2.2倍) |
| 実経路・有線のみ (Wi-Fi が届かない環境での検証) | 39〜54 Mbps (経路が無いときの単経路値) |

速度を決める定数 (`dualproxy.py` の先頭):

- `STREAM_WINDOW` (8MB) … ストリームごとの未確認予算。往復時間 × 帯域より大きく取ります。
  `SO_SNDBUF` を固定すると Windows の自動調整が止まり、Wi-Fi (RTT 12ms) で 160Mbps 程度に頭打ちになります
  (現在は固定していません)。
- `LINK_INFLIGHT` (8MB) / `QUEUE_BYTES` (8MB) … 链路あたりの未確認量とキュー上限。小さいと遅い経路で詰まります。
- `WINDOW_UPDATE` (256KB) / `ACK_BYTES` (256KB) / `ACK_FRAMES` … 確認応答と窓の間隔。既定を 64KB/32KB から
  4倍に緩めて、制御フレームの本数を減らしました (実測で +20〜70%)。
- `MAX_FRAME` (128KB, 受信上限) / `MAX_PAYLOAD` (32KB, 送信切り出し) … フレームの大きさ。
  受信は 128KB まで受け付けますが、送信は旧サーバー (32KB 超を拒否) と動くため 32KB に抑めています。
  **両 PC を同一の新しい版に更新したことを確認したら、両方の `MAX_PAYLOAD` を `128 * 1024` に上げてください**
  (フレーム数が 1/4 になり、ループバック換算でさらに速くなります)。片側だけ上げると旧サーバーがリンクを切ります。
- `WRITE_BATCH` (8) … 送信の詰まり確認 (drain) の間隔。フレームごとに待たないとイベントルの動きが減る。
  送り張りの量は `LINK_INFLIGHT` で有界なので、待ちは増えない。
- `STALL_RETRY` / `STALL_RESEND` … 遅い経路に捕まったフレームを他の経路でも送る閾値。
  受信側は通し番号で重複を捨てるので、順序の待ちを消せます。
- `--links` … 経路あたりのリンク数 (既定 2)。増やすと分割は細かくなりますが、再送と順序の管理が増えます。

実装側の优化 (2026-11-10 深夜, 定数とは別に効いたもの):

- フレームを `header + payload` の結合 bytes にせず、2回に分けて socket バッファに渡す
  (1フレームぶんのコピーが消える)。
- `resend_stalled` の走査を、古い順に並んでいるので「まだ若い」地点で打ち切る。
- `dualdl.py` は 1回の読みを 1MB に、共有ロックの取得を 4MB まばらに (統計の競合を減らす)。

大きなダウンロードはプロキシより `dualdl.py` の方が速いです (範囲分割は順序の待ちを作らない)。
ただし **TUN が有効な間は、送信元IPを固定しても TUN のデフォルトルートが勝つ**ため、`dualdl` の経路分離は
成立しません。使うなら TUN を止めてください:

```powershell
.\singbox\remove_tun.ps1
python dualdl.py https://proof.ovh.net/files/100Mb.dat --streams 4 --force
```

---

## 4. トラブルシュート

| 症状 | 確認コマンド | 対処 |
|---|---|---|
| 経路が 1 本しかない | `Get-NetTCPConnection -RemotePort 7443 -State Established \| Group-Object LocalAddress` | クライアントを再起動せず、`client.log` を見る。Wi-Fi が繋がっていない場合は `netsh wlan show interfaces` |
| Wi-Fi は繋がっているのに 0 本 (`経路 x.x.x.x が張れません`) | `python check_paths.py` (各 IP からゲートウェイとサーバーへの TCP を一行ずつ診断) | 回線ではなく AP の上り側が壊れている事が多い (DHCP は通るのに 192.168.3.1 が届かない等)。AP/ルーターの LAN 接続とクライアント分離設定を確認。 supervisors は 10 秒ごとに再接続を試みるので、復旧すれば自動的に束ねが戻る |
|アプリが失敗する | `curl.exe --socks5-hostname 127.0.0.1:1080 -o NUL https://example.com` | クライアントが生きているか。サーバー到達は `Test-NetConnection 192.168.3.22 -Port 7443` |
|トンネルが切れる/不安定 | `Get-Content singbox\tun-watch.log -Tail 30` | 連続失敗で stop されます。冷却後に自動で再開 |
| 認証失敗 | クライアントのレポートが `認証失敗` を表示 | `dualproxy.key` を両 PC で同じ内容に |
| ポート競合 | `Get-NetTCPConnection -LocalPort 1080 -State Listen` | 他のプロキシ(sing-box の別設定など)が 1080 を使っていないか |
| リンクが張れてすぐに切れる繰り返し (`backoff raised` がログに出る) | `Get-Content client.log -Tail 20` | 経路の途中が不安定なときの手順です (再接続の洪水を抑えるため間隔を 1s→5s に広げます)。経路が安定すれば戻ります |
|再起動後に動かない | `Get-ScheduledTask -TaskName dualproxy*` | `install_client.ps1` / `singbox\install_tun.ps1` / `install_server.ps1` を管理者で実行 |
| IPv6 のサイトだけ失敗する | `curl.exe -6 --socks5 127.0.0.1:1080 -o NUL -w "%{http_code}" https://www.cloudflare.com/` | サーバー PC が IPv6 で外に出られるか確認。出られない場合は `config.json` の TUN の `address` から IPv6 を外す |
| TUN が起動してすぐ落ちる (`sing-box exited code=1` が続く) | `.\singbox\sing-box.exe check -c singbox\config.json` | 設定の誤り。sing-box の版で使える項目か確認 (ログの出力先は `output`) |

ログの場所: `client.log`, `client.report.log` (本数と速度), `server.log` (サーバー側),
`singbox\sing-box.log` (sing-box 自身, 自動回転), `singbox\tun-watch.log` (TUN の判定)。
