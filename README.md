# RPZ-IR-Sensor API

RPZ-IR-Sensor Rev2の温度・湿度・気圧・照度取得と、38kHzのIR受信・保存・送信を行うPython/FastAPIサービスです。uvで依存を管理し、systemdで自動起動・障害時再起動します。

Raspberry Pi Zero 2 W / Debian 13 / Python 3.13.5で動作確認済みです。Rev1の照度センサーTSL2561には未対応です。実際の家電リモコンによる完全なコード取得と、再送による家電操作は未検証です。検証内容は[VERIFICATION.md](VERIFICATION.md)を参照してください。

## 構成

| 項目 | 配置・URL |
|---|---|
| ソース | `/opt/rpz-ir-sensor-api`（デプロイ用ユーザー所有） |
| 実行ユーザー | `rpz-ir-sensor`（ログイン不可のシステムユーザー） |
| 設定 | `/etc/rpz-ir-sensor-api.env`（root所有、デプロイ用ユーザーのグループで読み取り可、0640） |
| TLS証明書 | `/etc/rpz-ir-sensor-api/tls.crt`、`tls.key` |
| 記録・保存済みIRコード | `/var/lib/rpz-ir-sensor-api/codes.sqlite3` |
| API | `https://<host>` |
| ダッシュボード | `https://<host>/dashboard` |
| Swagger UI | `https://<host>/docs` |
| OpenAPI | `https://<host>/openapi.json` |

`<host>`は実機のホスト名（avahiが動いていれば`<ホスト名>.local`）かIPアドレスです。全IPv4インターフェースの443番ポートでHTTPSを待ち受けます。80番ポートのHTTPは、別サービス`rpz-ir-sensor-redirect`（`redirect.py`、標準ライブラリのみ）が同じパスのHTTPSへ308で転送するだけで、APIは提供しません。HTTPで送ったAPIキーは平文で流れるので、クライアントには最初から`https://`を指定してください。LAN内向けのサービスです。

証明書はインストーラーが生成する自己署名証明書です。初回接続時にブラウザが警告を出すので、インストーラーが表示したSHA-256フィンガープリントと一致することを確認してから進むか、`tls.crt`を端末に信頼済みとして登録してください。証明書にはホスト名、`<ホスト名>.local`、`localhost`、生成時のIPアドレスが入ります。IPアドレスが変わった場合は、`tls.key`と`tls.crt`を削除してインストーラーを再実行すると作り直せます。

## 新規導入

### 1. 前提条件と依存の準備

- Raspberry Pi、RPZ-IR-Sensor Rev2、I2C・GPIO IR用ドライバーを提供するOS。
- Python 3.13以上（OS標準のもの）、systemd、openssl。
- sudoを利用できるデプロイ用ユーザー。サービスはこのユーザーとは別の専用ユーザーで動きます。

以下はデプロイ用ユーザーで実行します。uvが導入済みならインストールを省略できます。

```bash
sudo apt-get update
sudo apt-get install -y git curl python3 openssl
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo install -d -o "$USER" -g "$USER" -m 755 /opt/rpz-ir-sensor-api
git clone https://github.com/skrtk98/rpz-ir-sensor-api.git /opt/rpz-ir-sensor-api
cd /opt/rpz-ir-sensor-api
UV_PYTHON_PREFERENCE=only-system ~/.local/bin/uv sync --locked --no-dev --compile-bytecode
```

uvの導入方法は[公式ドキュメント](https://docs.astral.sh/uv/getting-started/installation/)を参照してください。仮想環境はサービス用ユーザーから読める必要があるため、ホームディレクトリ内にインストールされるuv管理のPythonではなく、OS標準のPythonを使います。サービス用ユーザーは`/opt`に書き込めないため、`--compile-bytecode`でバイトコードを事前に生成します。

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
```

### 3. サービスの登録

LIRCデバイスが存在する状態で、デプロイ用ユーザーから実行します。`install.py`はboot設定やOSの再起動を行いません。

```bash
cd /opt/rpz-ir-sensor-api
sudo .venv/bin/python install.py
```

この操作は次のことを行い、最後にTLS証明書のフィンガープリントを表示します。

- `rpz-ir`グループとサービス用ユーザー`rpz-ir-sensor`を作成する（既にあれば何もしない）。
- udevルールを配置し、`/dev/rpz-ir-tx`と`/dev/rpz-ir-rx`を作成する。udevはドライバー名で送受信を判定し、デバイスを`rpz-ir`グループだけが読み書きできるようにする。
- 設定ファイルがなければ作成し、全権限用と読み取り用のAPIキーを生成する。既存の設定ファイルがあれば内容は保持し、読み取り用キーがなければ追加し、`RPZ_IR_TX`と`RPZ_IR_RX`は上記の固定デバイス名に書き直す。
- TLS証明書がなければ生成する。
- systemd unit（APIと転送用）を配置し、両サービスを有効化・再起動する。

新規作成時の設定では、外付けBME280の`0x76`を使います。外付けBME280がない場合は、次の「設定」を参照して`RPZ_BME280_ADDRESS=0x77`に変更してください。

### 旧構成（ホームディレクトリ配置）からの移行

旧構成ではサービスがデプロイ用ユーザーで動き、ソースはそのホームディレクトリにありました。上記の手順1の`/opt`への配置と`uv sync`を行ってから、手順3を実行してください。既存のAPIキー、記録、保存済みIRコードは引き継がれます。動作を確認したら、旧ディレクトリを削除できます。サービスは`video`グループを使わなくなるため、デプロイ用ユーザーを`i2c`・`video`グループに入れておく必要もありません。

## 設定

`sudoedit /etc/rpz-ir-sensor-api.env`で編集します。以下は「環境変数を省略した場合の実装」と「インストーラーが生成する設定」の違いです。

| 環境変数 | 未指定時の動作・値 | 生成する設定 |
|---|---|---|
| `RPZ_API_KEY` | 保護対象APIは503 | ランダムに生成 |
| `RPZ_READ_API_KEY` | 読み取り専用キーなし | ランダムに生成 |
| `RPZ_DB` | 作業ディレクトリ内の`data/codes.sqlite3` | `/var/lib/rpz-ir-sensor-api/codes.sqlite3` |
| `RPZ_BME280_ADDRESS` | `0x77`（基板上） | `0x76`（外付け） |
| `RPZ_IR_TX` | `/dev/lirc0` | `/dev/rpz-ir-tx` |
| `RPZ_IR_RX` | `/dev/lirc1` | `/dev/rpz-ir-rx` |
| `RPZ_SAMPLE_SECONDS` | `60`（`0`で記録停止） | 設定しない |
| `RPZ_RETENTION_DAYS` | `90` | 設定しない |

基板上の`0x77`はRaspberry Pi本体の発熱の影響を受けます。`/sensors/environment`ではクエリでどちらも指定できます。

systemdは設定ファイルを起動時に読み込みます。設定変更後はサービスを再起動してください。サービスが書き込めるのは`/var/lib/rpz-ir-sensor-api`（unitの`StateDirectory`）だけなので、DBはその中に置いてください。待受アドレス・ポートはunitの`ExecStart`で指定します。

## APIの利用

`/health`、ダッシュボード、APIドキュメントを除くAPIには`X-API-Key`ヘッダーが必要です。キーは2種類あります。

| キー | 使えるAPI |
|---|---|
| `RPZ_API_KEY` | すべて（IR受信・送信、保存コードの変更を含む） |
| `RPZ_READ_API_KEY` | センサー値の取得（`/sensors`、`/sensors/environment`、`/sensors/light`、`/sensors/history`）のみ |

キー未指定・不一致・権限外は401、サーバー側の`RPZ_API_KEY`未設定は503です。Swagger UIでは「Authorize」から設定ファイルのキーを入力します。

以下は実機のデプロイ用ユーザーで実行する例です。

```bash
. /etc/rpz-ir-sensor-api.env
alias rpz='curl --cacert /etc/rpz-ir-sensor-api/tls.crt'
rpz -H "X-API-Key: $RPZ_READ_API_KEY" https://localhost/sensors
rpz -H "X-API-Key: $RPZ_READ_API_KEY" 'https://localhost/sensors/environment?address=0x77'
```

IRの受信・保存は、次のコマンドの実行中にリモコンを受信機に向けて押してください。`name`を省略すると保存せずに波形を返します。**既存の名前を指定すると保存済みコードを上書きします。**

```bash
rpz -H "X-API-Key: $RPZ_API_KEY" -H 'Content-Type: application/json' \
  -d '{"timeout_seconds":10,"name":"tv_power"}' https://localhost/ir/receive
```

保存したコードの送信は家電を操作するため、送信先を確認して実行してください。

```bash
rpz -X POST -H "X-API-Key: $RPZ_API_KEY" https://localhost/ir/codes/tv_power/send
```

### エンドポイント

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/health` | プロセスの生存・デバイスの存在。測定成功は確認しません |
| GET | `/sensors` | 温度°C、湿度%、気圧hPa、照度lux、UTC日時 |
| GET | `/sensors/environment` | 温湿度・気圧。`address=0x76`または`0x77`を指定可能 |
| GET | `/sensors/light` | TSL2572の照度 |
| GET | `/sensors/history` | 記録済みの測定値。`hours`（既定24、最大8784）の範囲を最大`points`個（既定360、10〜2000）に平均して返します。`latest`は最新の記録です |
| POST | `/ir/receive` | IR受信。`timeout_seconds`は0.1〜30秒、既定10秒。任意の`name`で保存・上書き |
| POST | `/ir/send` | `{"pulses_us":[...]}`の生コードを38kHzで送信 |
| GET | `/ir/codes` | 保存コード一覧 |
| GET/PUT/DELETE | `/ir/codes/{name}` | 保存コード取得・登録/上書き・削除 |
| POST | `/ir/codes/{name}/send` | 保存コード送信 |

生コードはmark（発光）/space（待機）交互のマイクロ秒配列で、markで開始・終了します。1〜1023個の奇数個、各1〜100000us、合計1秒以内に制限します。コード名は英数字・`_`・`-`の1〜64文字です。

受信結果の`signal`は10個以下の波形なら`short`、それ以上なら`full`です。短い波形も取得・保存できますが、リピート信号や完全なコードと判定しているわけではありません。`full`もプロトコルの正当性や家電の動作を保証しません。

IR操作同士とセンサー操作同士はそれぞれ排他制御し、競合時は409です。定期記録もセンサー操作のため、記録中（毎分1秒未満）の`/sensors`系は409になることがあります。IR受信中でもセンサー取得はできます。受信タイムアウトは408、不正な波形は422、ハードウェア異常は503、存在しない保存コードは404です。

受信待ちには指定時間の上限を設け、連続ノイズで無期限に待ち続けることを防ぎます。設定処理・コード保存・HTTP応答などの時間は別途かかります。`measured_at`は測定処理の開始時に取得するUTC日時です。

## ダッシュボード

`https://<host>/dashboard`（`/`からも転送）で、最新値と温度・湿度・気圧・照度の推移を表示します。表示のみで、IR操作はできません。

サービスは起動中、`RPZ_SAMPLE_SECONDS`ごとに`/sensors`と同じ値を`RPZ_DB`の`readings`テーブルへ記録し、`RPZ_RETENTION_DAYS`を過ぎた記録を削除します。片方のセンサーだけ失敗した場合は、その値を空として記録します。ダッシュボードは記録を読むだけで、センサーには直接アクセスしません。

ページ自体は認証なしで配信しますが、データの取得には読み取り用キー`RPZ_READ_API_KEY`が必要です。入力したキーはそのブラウザのlocalStorageに保存されます。読み取り用キーではIR操作やコードの変更はできません。全権限用の`RPZ_API_KEY`はダッシュボードに入力しないでください。グラフ描画には同梱の[uPlot](https://github.com/leeoniya/uPlot) 1.6.32（MIT、`static/vendor/`）を使い、外部CDNには依存しません。

## 開発・運用

以下は実機のデプロイ用ユーザーで実行します。

### 状態・ログの確認

```bash
systemctl status rpz-ir-sensor-api rpz-ir-sensor-redirect --no-pager
journalctl -u rpz-ir-sensor-api -n 100 --no-pager
curl --cacert /etc/rpz-ir-sensor-api/tls.crt https://localhost/health
```

### 開発用依存とチェック

開発用依存を入れた仮想環境は、チェック後に`--no-dev`で戻してください。

```bash
cd /opt/rpz-ir-sensor-api
~/.local/bin/uv sync --locked
~/.local/bin/uv run --locked pytest -q
~/.local/bin/uv run --locked ruff check app.py tests
~/.local/bin/uv sync --locked --no-dev --compile-bytecode
```

### ソース更新

以下はソースと依存を更新し、サービスを再起動する操作です。

```bash
cd /opt/rpz-ir-sensor-api
git pull --ff-only
~/.local/bin/uv sync --locked --no-dev --compile-bytecode
sudo systemctl restart rpz-ir-sensor-api
```

### 設定変更後の再起動

systemdは設定ファイルを起動時に読み込みます。

```bash
sudo systemctl restart rpz-ir-sensor-api
```

### unit・udevの再登録

unitかudevルールを変更した場合に実行します。既存のAPIキーと証明書は保持し、IRデバイスの設定を書き直してサービスを再起動します。

```bash
cd /opt/rpz-ir-sensor-api
sudo .venv/bin/python install.py
```

サービスは専用ユーザー`rpz-ir-sensor`と`i2c`・`rpz-ir`グループで動作します。443番・80番で待ち受けるための`CAP_NET_BIND_SERVICE`以外の特権は持たず、`/home`を参照できず、I2CとLIRC以外のデバイスにはアクセスできません。ハードウェアの競合防止のため、uvicornは必ず1 workerで実行します。APIキー、TLS鍵、SQLiteデータはGit対象外で、`uv.lock`はGit管理対象です。

## 参考

- [RPZ-IR-Sensor製品資料](https://www.indoorcorgielec.com/products/rpz-ir-sensor/)
- [cgsensor](https://github.com/IndoorCorgi/cgsensor)
- [cgir](https://github.com/IndoorCorgi/cgir)
