# RPZ-IR-Sensor API

RPZ-IR-Sensor Rev2の温度・湿度・気圧・照度取得と、38kHzのIR受信・保存・送信を行うPython/FastAPIサービスです。uvで依存を管理し、systemdで自動起動・障害時再起動します。

Raspberry Pi Zero 2 W / Debian 13 / Python 3.13.5で動作確認済みです。Rev1の照度センサーTSL2561には未対応です。実際の家電リモコンによる完全なコード取得と、再送による家電操作は未検証です。検証内容は[VERIFICATION.md](VERIFICATION.md)を参照してください。

## 現在の配置

| 項目 | 配置・URL |
|---|---|
| ソース | `/home/ops/rpz-ir-sensor-api` |
| 設定 | `/etc/rpz-ir-sensor-api.env`（ops所有、0600） |
| 保存済みIRコード | `/var/lib/rpz-ir-sensor-api/codes.sqlite3` |
| API | `http://192.168.1.252:8000` |
| Swagger UI | `http://192.168.1.252:8000/docs` |
| OpenAPI | `http://192.168.1.252:8000/openapi.json` |

URLのIPアドレスは現在の実機のものです。systemdの設定では全IPv4インターフェースの8000番ポートで待ち受けます。LAN内向けのHTTPサービスです。

## 新規導入

既存の実機には導入済みです。以下は新しい環境に導入する場合の手順です。

### 1. 前提条件と依存の準備

- Raspberry Pi、RPZ-IR-Sensor Rev2、I2C・GPIO IR用ドライバーを提供するOS。
- Python 3.13以上、systemd、sudoを利用できる`ops`ユーザー。
- `i2c`と`video`グループが存在すること。
- `ops`ユーザーでGitHubに接続できるSSH別名`github.com-SKRTK`の設定。

unitと`install.py`はユーザー名`ops`と配置先`/home/ops/rpz-ir-sensor-api`を固定しています。別のユーザーや配置先を使う場合は、両方のファイルを修正してください。

以下は`ops`ユーザーで実行します。uvが導入済みならインストールを省略できます。

```bash
sudo apt-get update
sudo apt-get install -y git curl python3
sudo usermod -aG i2c,video ops
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone github.com-SKRTK:skrtk98/rpz-ir-sensor-api.git /home/ops/rpz-ir-sensor-api
cd /home/ops/rpz-ir-sensor-api
/home/ops/.local/bin/uv sync --locked --no-dev
```

uvの導入方法は[公式ドキュメント](https://docs.astral.sh/uv/getting-started/installation/)を参照してください。

### 2. ハードウェア設定

`sudoedit /boot/firmware/config.txt`で、`[all]`セクションに以下を設定してください。既存の設定を確認し、同じGPIOに競合する設定を追加しないでください。

```ini
[all]
dtparam=i2c_arm=on
dtoverlay=gpio-ir,gpio_pin=4
dtoverlay=gpio-ir-tx,gpio_pin=13
```

設定後に再起動します。

```bash
sudo reboot
```

再接続後、I2CとIRのデバイスを確認します。IRのデバイス番号は固定とは限りません。

```bash
ls -l /dev/i2c-1 /dev/lirc*
id ops
```

### 3. サービスの登録

LIRCデバイスが存在する状態で実行します。`install.py`はboot設定やOSの再起動を行いません。

```bash
cd /home/ops/rpz-ir-sensor-api
sudo .venv/bin/python install.py
```

この操作はudevルールとsystemd unitを配置し、サービスを有効化・起動・再起動します。udevはドライバー名で送受信を判定し、`/dev/rpz-ir-tx`と`/dev/rpz-ir-rx`を作成します。

設定ファイルがなければAPIキーを生成し、外付けBME280の`0x76`を設定します。既存の設定ファイルがあればAPIキー等は保持しますが、`RPZ_IR_TX`と`RPZ_IR_RX`は上記の固定デバイス名に書き直します。

外付けBME280がない場合は、次の「設定」を参照して`RPZ_BME280_ADDRESS=0x77`に変更してください。

## 設定

`sudoedit /etc/rpz-ir-sensor-api.env`で編集します。以下は「環境変数を省略した場合の実装」と「インストーラーが生成する設定」の違いです。

| 環境変数 | 未指定時の動作・値 | 生成する設定 |
|---|---|---|
| `RPZ_API_KEY` | 保護対象APIは503 | ランダムに生成 |
| `RPZ_DB` | 作業ディレクトリ内の`data/codes.sqlite3` | `/var/lib/rpz-ir-sensor-api/codes.sqlite3` |
| `RPZ_BME280_ADDRESS` | `0x77`（基板上） | `0x76`（外付け） |
| `RPZ_IR_TX` | `/dev/lirc0` | `/dev/rpz-ir-tx` |
| `RPZ_IR_RX` | `/dev/lirc1` | `/dev/rpz-ir-rx` |

現在の実機では外付けセンサーの`0x76`を設定しています。基板上の`0x77`はRaspberry Pi本体の発熱の影響を受けます。`/sensors/environment`ではクエリでどちらも指定できます。

systemdは設定ファイルを起動時に読み込みます。設定変更後はサービスを再起動してください。DBの保存先を変更する場合は、unitの`ReadWritePaths`と書き込み権限も調整する必要があります。待受アドレス・ポートはunitの`ExecStart`で指定します。

## APIの利用

`/health`とAPIドキュメントを除くAPIには`X-API-Key`ヘッダーが必要です。キー未指定・不一致は401、サーバー側のキー未設定は503です。Swagger UIでは「Authorize」から設定ファイルのAPIキーを入力します。

以下はSSH先の`ops`ユーザーで実行する例です。

```bash
. /etc/rpz-ir-sensor-api.env
curl -H "X-API-Key: $RPZ_API_KEY" http://localhost:8000/sensors
curl -H "X-API-Key: $RPZ_API_KEY" 'http://localhost:8000/sensors/environment?address=0x77'
```

IRの受信・保存は、次のコマンドの実行中にリモコンを受信機に向けて押してください。`name`を省略すると保存せずに波形を返します。**既存の名前を指定すると保存済みコードを上書きします。**

```bash
curl -H "X-API-Key: $RPZ_API_KEY" -H 'Content-Type: application/json' \
  -d '{"timeout_seconds":10,"name":"tv_power"}' http://localhost:8000/ir/receive
```

保存したコードの送信は家電を操作するため、送信先を確認して実行してください。

```bash
curl -X POST -H "X-API-Key: $RPZ_API_KEY" http://localhost:8000/ir/codes/tv_power/send
```

### エンドポイント

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/health` | プロセスの生存・デバイスの存在。測定成功は確認しません |
| GET | `/sensors` | 温度°C、湿度%、気圧hPa、照度lux、UTC日時 |
| GET | `/sensors/environment` | 温湿度・気圧。`address=0x76`または`0x77`を指定可能 |
| GET | `/sensors/light` | TSL2572の照度 |
| POST | `/ir/receive` | IR受信。`timeout_seconds`は0.1〜30秒、既定10秒。任意の`name`で保存・上書き |
| POST | `/ir/send` | `{"pulses_us":[...]}`の生コードを38kHzで送信 |
| GET | `/ir/codes` | 保存コード一覧 |
| GET/PUT/DELETE | `/ir/codes/{name}` | 保存コード取得・登録/上書き・削除 |
| POST | `/ir/codes/{name}/send` | 保存コード送信 |

生コードはmark（発光）/space（待機）交互のマイクロ秒配列で、markで開始・終了します。1〜1023個の奇数個、各1〜100000us、合計1秒以内に制限します。コード名は英数字・`_`・`-`の1〜64文字です。

受信結果の`signal`は10個以下の波形なら`short`、それ以上なら`full`です。短い波形も取得・保存できますが、リピート信号や完全なコードと判定しているわけではありません。`full`もプロトコルの正当性や家電の動作を保証しません。

IR操作同士とセンサー操作同士はそれぞれ排他制御し、競合時は409です。IR受信中でもセンサー取得はできます。受信タイムアウトは408、不正な波形は422、ハードウェア異常は503、存在しない保存コードは404です。

受信待ちには指定時間の上限を設け、連続ノイズで無期限に待ち続けることを防ぎます。設定処理・コード保存・HTTP応答などの時間は別途かかります。`measured_at`は測定処理の開始時に取得するUTC日時です。

## 開発・運用

### 状態・ログの確認

```bash
systemctl status rpz-ir-sensor-api --no-pager
journalctl -u rpz-ir-sensor-api -n 100 --no-pager
curl http://localhost:8000/health
```

### 開発用依存とチェック

```bash
cd /home/ops/rpz-ir-sensor-api
/home/ops/.local/bin/uv sync --locked
/home/ops/.local/bin/uv run --locked pytest -q
/home/ops/.local/bin/uv run --locked ruff check app.py tests
```

### ソース更新

以下はソースと依存を更新し、サービスを再起動する操作です。

```bash
cd /home/ops/rpz-ir-sensor-api
git pull --ff-only
/home/ops/.local/bin/uv sync --locked --no-dev
sudo systemctl restart rpz-ir-sensor-api
```

### 設定変更後の再起動

```bash
sudo systemctl restart rpz-ir-sensor-api
```

unit自体を変更した場合は、次の再登録を行います。

### unit・udevの再登録

既存APIキーは保持しますが、IRデバイスの設定を書き直してサービスを再起動します。

```bash
cd /home/ops/rpz-ir-sensor-api
sudo .venv/bin/python install.py
```

systemdは非rootの`ops`ユーザーと`i2c`/`video`グループで動作します。ハードウェアの競合防止のためuvicornは必ず1 workerで実行します。APIキーとSQLiteデータはGit対象外、`uv.lock`はGit管理対象です。

## 参考

- [RPZ-IR-Sensor製品資料](https://www.indoorcorgielec.com/products/rpz-ir-sensor/)
- [cgsensor](https://github.com/IndoorCorgi/cgsensor)
- [cgir](https://github.com/IndoorCorgi/cgir)
