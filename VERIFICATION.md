# 検証記録

2026-10-11 JST、`rpz-ir-sensor` (Raspberry Pi Zero 2 W / Debian 13 / Python 3.13.5)で検証。

- `uv sync`でuv.lock生成、依存解決済み。
- `uv run ruff check app.py tests`: 合格。
- `uv run pytest -q`: 13件合格。Starletteのhttpxテストクライアントに非推奨警告が1件。
- systemdの有効化・起動・再起動を確認。
- LAN (`192.168.1.252:8000`)からhealth取得成功。
- APIキー未指定は401。キー未設定は503 (自動テスト)。
- 外付けBME280 0x76: 23.1°C / 48.3% / 1016.0hPa。
- 基板上BME280 0x77: 28.9°C / 29.4% / 1015.1hPa。
- TSL2572: 337.8lux。
- IR送信ドライバーのpulse/carrier機能と受信ドライバーのmode2/timeout機能を確認。
- IR受信0.2秒指定: 408を約0.29秒で返却。
- テスト用IR波形の保存・送信・削除成功。
- IR受信中のセンサー取得成功、同時IR送信は409。
- 自身のIR送信光の受信APIは200。ただし短い一部分のみ (`signal=short`)。

実際の家電リモコンを使った完全な波形の取得と再送による家電動作は未検証。
基板上での自己受信は光学的条件や受信機のAGCの影響を受けるため、
READMEの受信例で実際のリモコンを使って確認してください。
OS自体の再起動は行っていません。boot設定とudevルールを永続化済みです。
