# doublecheckun-drugmaster

スマートフォンアプリ「ダブルチェックん」（iOS / Android）で、医薬品バーコード（GTIN）から販売名・規格・製造販売元を表示するためのマスターデータ配信パイプラインです。

一般財団法人医療情報システム開発センター（MEDIS-DC）が公開するデータを毎週監視し、新しい版が出たら SQLite に変換して GitHub Release で配信します。

## データソース

| ソース | URL | 用途 |
|---|---|---|
| 医薬品コード登録システム 全件ファイル②（調剤・販売・元梱包装単位コード） | https://medhot.medd.jp/view_download | GTIN → 販売名・規格・包装・メーカー |
| 医薬品HOTコードマスター HOT13 販売包装単位コード付版 | https://www2.medis.or.jp/hcode/ | 製造会社・販売会社の補完 |

出典: 医薬品HOTコードマスターは、厚生労働省の委託を受けて、一般財団法人医療情報システム開発センターにより作成されたものである。

このリポジトリは MEDIS の公開データを加工（データベース形式に変換）したものを配布します。加工後のデータの内容について MEDIS-DC は責任を負いません。

## 仕組み

1. `.github/workflows/update.yml` が毎週月曜 09:00 JST に実行（手動実行も可）
2. `scripts/drugmaster.py check` が 2 サイトの最新版日付を取得し、`state.json` と比較
3. 新版があれば `build` で ZIP を取得、Shift_JIS をデコードして `drugmaster.sqlite` を生成、`verify` で検査
4. Release `vYYYYMMDD` に `drugmaster.sqlite.gz` を添付し、`manifest.json` と `state.json` を更新してコミット
5. 失敗時は Issue を自動作成

アプリは `manifest.json` を取得して `source_date` が手元より新しければ `db_url` の DB をダウンロードし、`sha256_gz` で検証して差し替えます。

- manifest: `https://raw.githubusercontent.com/Myuta7/doublecheckun-drugmaster/main/manifest.json`
- DB: `https://github.com/Myuta7/doublecheckun-drugmaster/releases/download/vYYYYMMDD/drugmaster.sqlite.gz`

## SQLite スキーマ

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- schema_version, source_date, medhot_date, hot_date, built_at, record_count, attribution

CREATE TABLE drugs (
  gtin            TEXT PRIMARY KEY,  -- 14 桁
  package_level   INTEGER NOT NULL,  -- 1=調剤包装単位 2=販売包装単位 3=元梱包装単位
  brand_name      TEXT NOT NULL,     -- 販売名
  spec            TEXT,              -- 規格
  package_form    TEXT,              -- 包装形態（PTP など）
  package_qty     TEXT,              -- 包装数量（調剤: 1 シート分、販売/元梱: 総数量）
  category        TEXT,              -- 内用薬/注射薬/外用薬/歯科用薬
  dosage_form     TEXT,              -- 剤形
  maker           TEXT,              -- データ登録企業（略称）
  manufacturer    TEXT,              -- 製造会社（HOT から補完、無い場合は空）
  seller          TEXT,              -- 販売会社（HOT から補完、無い場合は空）
  yj_code         TEXT,              -- 薬価コード
  discontinued_on TEXT,              -- 販売中止年月日 YYYYMMDD
  last_lot_expiry TEXT,              -- 最終ロット使用期限 YYYYMM
  updated_on      TEXT               -- 更新年月日 YYYYMMDD
) WITHOUT ROWID;
```

同じ GTIN が複数行に現れる場合（調剤包装単位コードは販売包装ごとに重複する）は、販売中止でない行、更新日が新しい行を優先して 1 件にまとめています。

## ローカル実行

```bash
python3 scripts/drugmaster.py check
python3 scripts/drugmaster.py build --out dist            # 最新版を取得して生成
python3 scripts/drugmaster.py verify --db dist/drugmaster.sqlite
```

標準ライブラリのみで動作します（Python 3.10 以上）。
