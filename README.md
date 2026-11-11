# Wi-Fi + Ethernet 同時利用プロキシ

Windows PCのWi-Fiと有線LANを同時に使い、1本のTCP接続でも両方の帯域を束ねるためのツールです。
同じLAN内に置いた中継サーバー(自宅PCなど)まで、両方の回線でリンクを張り、データを分割して送り、サーバー側で組み立て直します。

```
アプリ ─ sing-box (TUN) ─ SOCKS5 ─ dualproxy クライアント ═╦═ Wi-Fi ═══╦═ dualproxy サーバー ─ インターネット
                                                          ╚═ 有線LAN ═╝
```

- 回線の片方が切れても、通信は続きます(未到達のデータは残った回線で再送)。
- 通信はLAN内の中継だけで、**暗号化されません**。サーバーのポート(7443)はルーターで外部に開けないでください。
- UDP(ゲーム・通話)とLAN内宛ては直接通信します。HTTP/3(UDP 443)は止めて、TCPへ切り替えさせます。

## 必要なもの
- Windows 10/11、Python 3.10以上
- 同じLAN内で常時動くPC(中継サーバー)
- 全アプリを経由させる場合: [sing-box](https://github.com/SagerNet/sing-box) 1.14 の `sing-box.exe` を `singbox\` に置く

## 簡単な使い方

**1. 共有鍵を作る(両方のPCに同じものを置く)**
```bash
py -c "import secrets;open('dualproxy.key','w').write(secrets.token_urlsafe(32))"
```

**2. サーバーPCで起動**
```bash
py dualproxy.py server --secret-file dualproxy.key
```
ファイアウォールでTCP 7443をLAN内だけに許可してください。

**3. クライアントPCで起動**(Wi-Fiと有線の経路は自動検出)
```bash
py dualproxy.py client 192.168.x.x --secret-file dualproxy.key
```
`127.0.0.1:1080` がSOCKS5プロキシになります。動作確認:
```bash
curl -x socks5h://127.0.0.1:1080 -o NUL https://speed.cloudflare.com/__down?bytes=100000000
```

**4. 全アプリを自動で経由させる(任意)**
管理者のPowerShellで `singbox\install_tun.ps1` を実行すると、sing-boxの仮想アダプターで全TCPがプロキシを通ります。
トンネルが使えないときは自動でsing-boxを止め、通常の通信に戻ります。外すときは `singbox\remove_tun.ps1`。

自動起動の設定(`install_client.ps1` / `install_server.ps1`)やトラブルシューティングは [SETUP.md](SETUP.md) を見てください。

## 単発ダウンロードだけなら
中継サーバーなしで、HTTPのRange要求を両回線に振り分けて1ファイルを落とせます。
```bash
py dualdl.py https://example.com/large.iso
```

## テスト
```bash
py -m unittest test_dualproxy test_dualdl
```

## 注意
- 効果は回線の組み合わせ次第です。片方が極端に遅いと、束ねないほうが速いこともあります。
- 外から見たIPv6アドレスは、TCP(サーバー経由)とUDP(直接)で変わります。これを嫌うサービスがあれば、そのアプリをトンネルの対象外にしてください。

## ライセンス
MIT License ([LICENSE](LICENSE))
