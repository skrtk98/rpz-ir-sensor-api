# RPZ-IR-Sensor API

Raspberry Pi Zero 2 W + RPZ-IR-Sensor Rev2向けPython/FastAPIサービス。
uvで依存を固定し、systemdで自動起動・障害時再起動します。

## 配置と実行

- ソース: `/home/ops/rpz-ir-sensor-api`
- 設定: `/etc/rpz-ir-sensor-api.env` (ops所有、0600)
- 保存済みIRコード: `/var/lib/rpz-ir-sensor-api/codes.sqlite3`
- API: `http://192.168.1.252:8000`
- Swagger UI: `http://192.168.1.252:8000/docs` (AuthorizeからAPIキー入力)
- OpenAPI: `/openapi.json`

`/health`とAPIドキュメントを除くAPIには`X-API-Key`ヘッダーが必要です。
キー未設定時は503を返します。LAN内向けHTTPサービスです。

SSH先で以下を実行できます (APIキーを画面表示しません)。

```bash
set -a
. /etc/rpz-ir-sensor-api.env
set +a
curl -H "X-API-Key: $RPZ_API_KEY" http://localhost:8000/sensors
curl -H "X-API-Key: $RPZ_API_KEY" 'http://localhost:8000/sensors/environment?address=0x77'
curl -H "X-API-Key: $RPZ_API_KEY" -H 'Content-Type: application/json' \
  -d '{"timeout_seconds":10,"name":"tv_power"}' http://localhost:8000/ir/receive
# 受信待ち中にリモコンを基板の受信機に向けて押してください。
curl -X POST -H "X-API-Key: $RPZ_API_KEY" http://localhost:8000/ir/codes/tv_power/send
```

## エンドポイント

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/health` | プロセスの生存・デバイスの存在 (測定成功の保証ではありません) |
| GET | `/sensors` | 温度°C、湿度%、気圧hPa、照度lux、UTC測定日時 |
| GET | `/sensors/environment` | 温湿度・気圧。`address=0x76`または`0x77`を指定可能 |
| GET | `/sensors/light` | TSL2572の照度 |
| POST | `/ir/receive` | 0.1〜30秒以内に受信。任意の`name`指定で保存 |
| POST | `/ir/send` | `{"pulses_us":[...]}`の生コードを38kHzで送信 |
| GET | `/ir/codes` | 保存コード一覧 |
| GET/PUT/DELETE | `/ir/codes/{name}` | 保存コード取得・登録/更新・削除 |
| POST | `/ir/codes/{name}/send` | 保存コード送信 |

生コードはmark/space交互のマイクロ秒配列で、markで開始・終了します。
1〜1023個、奇数個、各1〜100000us、合計1秒以内に制限します。
受信結果の`signal`は10個以下の短い波形なら`short`、それ以上なら`full`です。
短いリピート信号も取得・保存できます。`full`もプロトコルの正当性を保証しません。
コード名は英数字・`_`・`-`の1〜64文字。保存操作はSQLiteトランザクションを使います。
IR受信・送信は排他制御し、競合時409、受信タイムアウト408、不正な波形422、
ハードウェア異常503を返します。IR受信中にもセンサーAPIは利用できます。
連続ノイズでも受信待ちは指定時間以内に終了します。

## ハードウェア設定

`/boot/firmware/config.txt`の`[all]`に以下を設定済みです。

```ini
dtparam=i2c_arm=on
dtoverlay=gpio-ir,gpio_pin=4
dtoverlay=gpio-ir-tx,gpio_pin=13
```

既存設定のバックアップ: `/boot/firmware/config.txt.before-rpz-api`。
初回は`dtoverlay`で実行中のOSにも適用。次回起動から上記設定が使用されます。
udevルール`/etc/udev/rules.d/99-rpz-ir-sensor.rules`でドライバー名を判定し、
`/dev/rpz-ir-tx`と`/dev/rpz-ir-rx`を作成します。番号が変わっても設定は維持されます。
Rev1のTSL2561には未対応です。

`RPZ_BME280_ADDRESS=0x76`を既定としています (実機で確認済みの外付けセンサー)。
基板上0x77も指定可能です。基板上はRaspberry Piの発熱の影響を受けます。
設定変更後は`sudo systemctl restart rpz-ir-sensor-api`を実行してください。

## 開発・運用

```bash
cd /home/ops/rpz-ir-sensor-api
/home/ops/.local/bin/uv sync --locked
/home/ops/.local/bin/uv run pytest -q
/home/ops/.local/bin/uv run ruff check app.py tests
sudo systemctl status rpz-ir-sensor-api
sudo systemctl restart rpz-ir-sensor-api
sudo journalctl -u rpz-ir-sensor-api -n 100 --no-pager
# unit・udev・初期設定の再導入 (既存APIキーは保持)
sudo .venv/bin/python install.py
```

systemdは非rootのopsユーザー、i2c/videoグループで動作します。
ハードウェアの競合防止のためuvicornは必ず1 workerで実行します。
APIキーとSQLiteデータはGit対象外です。uv.lockはGitで管理します。
Gitのlocal identityは`skrtk98 <skrtk980665@gmail.com>`です。

## 参考

- https://www.indoorcorgielec.com/products/rpz-ir-sensor/
- https://github.com/IndoorCorgi/cgsensor
- https://github.com/IndoorCorgi/cgir
