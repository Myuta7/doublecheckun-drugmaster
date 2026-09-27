#!/usr/bin/env python3
"""ダブルチェックん 医薬品マスター パイプライン

MEDIS-DC が公開する 2 つのデータソースを監視し、GTIN → 医薬品名 を引くための
SQLite データベースを生成する。標準ライブラリのみで動作する。

  データソース
    medhot : 医薬品コード登録システム  https://medhot.medd.jp/view_download
             全件：調剤包装単位・販売包装単位・元梱包装単位コードファイル A_YYYYMMDD_2.zip
    hot    : 医薬品HOTコードマスター    https://www2.medis.or.jp/hcode/
             HOT13 販売包装単位コード付版 hYYYYMMDD_h.zip（製造会社・販売会社の補完に使用）

  サブコマンド
    check                    2 サイトの最新版日付を取得し JSON で出力
    build --out DIR          最新版（または --medhot-date/--hot-date 指定版）から DB を生成
    verify --db FILE         生成済み DB の妥当性検査

出典: 医薬品HOTコードマスターは、厚生労働省の委託を受けて、
      一般財団法人医療情報システム開発センターにより作成されたものである。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA_VERSION = 1
USER_AGENT = "doublecheckun-drugmaster/1.0 (+https://github.com/Myuta7/doublecheckun-drugmaster)"

MEDHOT_PAGE = "https://medhot.medd.jp/view_download"
MEDHOT_ZIP = "https://medhot.medd.jp/csv/A_{date}_2.zip"
MEDHOT_RE = re.compile(r"A_(\d{8})_2\.zip")

HOT_PAGE = "https://www2.medis.or.jp/hcode/"
HOT_ZIP = "https://www2.medis.or.jp/hcode/moto_data/h{date}_h.zip"
HOT_RE = re.compile(r"moto_data/h(\d{8})_h\.zip")

ATTRIBUTION = (
    "医薬品HOTコードマスターは、厚生労働省の委託を受けて、"
    "一般財団法人医療情報システム開発センターにより作成されたものである。"
)

# 医薬品コード登録システム ファイル②（44 列）の列番号（0 始まり）
class M:
    UPDATED = 1
    MAKER = 2
    BRAND = 3
    NOTICE_CLASS = 7
    YJ = 8
    SPEC = 12
    CATEGORY = 15
    FORM = 16
    PKG_FORM = 22
    PKG_QTY = 23
    PKG_UNIT = 24
    TOTAL_QTY = 25
    TOTAL_UNIT = 26
    GTIN_DISPENSE = 29
    DISPENSE_NAME = 30
    GTIN_SALES = 32
    GTIN_CASE = 33
    DISCONTINUED = 42
    LAST_LOT_EXPIRY = 43
    NCOLS = 44

# HOT13 販売包装単位コード付版（25 列）の列番号
class H:
    HOT13 = 0
    MANUFACTURER = 20
    SELLER = 21
    GTIN_SALES = 24
    NCOLS = 25

LEVEL_DISPENSE, LEVEL_SALES, LEVEL_CASE = 1, 2, 3


# ---------------------------------------------------------------- utilities
def log(msg: str) -> None:
    print(f"[drugmaster] {msg}", file=sys.stderr, flush=True)


def fetch(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def gtin_check_digit_ok(code: str) -> bool:
    if len(code) != 14 or not code.isdigit():
        return False
    s = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(code[:13]))
    return (10 - s % 10) % 10 == int(code[13])


def fmt_qty(num: str, unit: str) -> str:
    num = num.strip()
    if num.endswith(".000"):
        num = num[:-4]
    return f"{num}{unit.strip()}".strip()


def read_csv_from_zip(zip_bytes: bytes, name_pattern: re.Pattern, ncols: int) -> list[list[str]]:
    """ZIP 内の指定ファイルを Shift_JIS(cp932) として読み、列数を検証して返す。"""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = [n for n in z.namelist() if name_pattern.search(n)]
        if not names:
            raise RuntimeError(f"ZIP 内に {name_pattern.pattern} が見つかりません: {z.namelist()}")
        raw = z.read(names[0])
    text = raw.decode("cp932", errors="strict")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    bad = [i for i, r in enumerate(rows) if len(r) != ncols]
    if bad:
        raise RuntimeError(f"列数不一致 {len(bad)} 行（期待 {ncols}）: 例 行{bad[0]} = {len(rows[bad[0]])} 列")
    return rows


# ---------------------------------------------------------------- check
def latest_dates() -> dict:
    out = {}
    html = fetch(MEDHOT_PAGE).decode("utf-8", errors="replace")
    m = sorted(set(MEDHOT_RE.findall(html)))
    if not m:
        raise RuntimeError("medhot: ダウンロードページから A_YYYYMMDD_2.zip を検出できません（ページ構造が変わった可能性）")
    out["medhot_date"] = m[-1]

    html = fetch(HOT_PAGE).decode("cp932", errors="replace")
    h = sorted(set(HOT_RE.findall(html)))
    if not h:
        raise RuntimeError("hot: トップページから hYYYYMMDD_h.zip を検出できません（ページ構造が変わった可能性）")
    out["hot_date"] = h[-1]
    return out


# ---------------------------------------------------------------- build
def build_db(medhot_zip: bytes, hot_zip: bytes | None, medhot_date: str, hot_date: str | None, out_dir: Path) -> dict:
    rows = read_csv_from_zip(medhot_zip, re.compile(r"A_\d{8}_2\.txt$", re.I), M.NCOLS)
    header, body = rows[0], rows[1:]
    if header[M.GTIN_DISPENSE] != "調剤包装単位コード" or header[M.GTIN_SALES] != "販売包装単位コード":
        raise RuntimeError(f"medhot ヘッダーが想定と異なります: {header[M.GTIN_DISPENSE]!r}, {header[M.GTIN_SALES]!r}")
    log(f"medhot rows={len(body)}")

    # HOT から 販売包装単位コード → (製造会社, 販売会社)
    hot_seller: dict[str, tuple[str, str]] = {}
    if hot_zip is not None:
        hrows = read_csv_from_zip(hot_zip, re.compile(r"MEDIS\d{8}_h\.txt$", re.I), H.NCOLS)
        if hrows[0][H.GTIN_SALES] != "販売包装単位コード":
            raise RuntimeError(f"hot ヘッダーが想定と異なります: {hrows[0][H.GTIN_SALES]!r}")
        for r in hrows[1:]:
            g = r[H.GTIN_SALES].strip()
            if g and g not in hot_seller:
                hot_seller[g] = (r[H.MANUFACTURER].strip(), r[H.SELLER].strip())
        log(f"hot rows={len(hrows) - 1} sellers={len(hot_seller)}")

    # 1 GTIN につき 1 レコードに正規化。優先: 販売中止でない > 更新日が新しい > 先勝ち
    candidates: dict[str, list[tuple[tuple, dict]]] = defaultdict(list)
    stats = Counter()
    for r in body:
        base = {
            "brand_name": r[M.BRAND].strip(),
            "spec": r[M.SPEC].strip(),
            "package_form": r[M.PKG_FORM].strip(),
            "category": r[M.CATEGORY].strip(),
            "dosage_form": r[M.FORM].strip(),
            "maker": r[M.MAKER].replace("　", " ").strip(),
            "yj_code": r[M.YJ].strip(),
            "discontinued_on": r[M.DISCONTINUED].strip(),
            "last_lot_expiry": r[M.LAST_LOT_EXPIRY].strip(),
            "updated_on": r[M.UPDATED].strip(),
        }
        sales_gtin = r[M.GTIN_SALES].strip()
        manu, seller = hot_seller.get(sales_gtin, ("", ""))
        base["manufacturer"] = manu
        base["seller"] = seller
        rank = (1 if base["discontinued_on"] else 0, -int(base["updated_on"] or 0))

        for level, idx, qty in (
            (LEVEL_DISPENSE, M.GTIN_DISPENSE, fmt_qty(r[M.PKG_QTY], r[M.PKG_UNIT])),
            (LEVEL_SALES, M.GTIN_SALES, fmt_qty(r[M.TOTAL_QTY], r[M.TOTAL_UNIT])),
            (LEVEL_CASE, M.GTIN_CASE, fmt_qty(r[M.TOTAL_QTY], r[M.TOTAL_UNIT])),
        ):
            g = r[idx].strip()
            if not g:
                continue
            if not gtin_check_digit_ok(g):
                stats["bad_check_digit"] += 1
                continue
            rec = dict(base, gtin=g, package_level=level, package_qty=qty)
            candidates[g].append((rank, rec))

    records = []
    for g, lst in candidates.items():
        lst.sort(key=lambda t: t[0])
        records.append(lst[0][1])
        if len(lst) > 1:
            stats["dup_gtin"] += 1
    records.sort(key=lambda d: d["gtin"])
    stats["records"] = len(records)
    stats["level_1"] = sum(1 for d in records if d["package_level"] == 1)
    stats["level_2"] = sum(1 for d in records if d["package_level"] == 2)
    stats["level_3"] = sum(1 for d in records if d["package_level"] == 3)
    log(f"records={stats['records']} dup_gtin={stats['dup_gtin']} bad_cd={stats['bad_check_digit']}")

    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "drugmaster.sqlite"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        PRAGMA page_size = 4096;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE drugs (
          gtin            TEXT PRIMARY KEY,
          package_level   INTEGER NOT NULL,
          brand_name      TEXT NOT NULL,
          spec            TEXT,
          package_form    TEXT,
          package_qty     TEXT,
          category        TEXT,
          dosage_form     TEXT,
          maker           TEXT,
          manufacturer    TEXT,
          seller          TEXT,
          yj_code         TEXT,
          discontinued_on TEXT,
          last_lot_expiry TEXT,
          updated_on      TEXT
        ) WITHOUT ROWID;
        """
    )
    con.executemany(
        """INSERT INTO drugs VALUES (:gtin,:package_level,:brand_name,:spec,:package_form,:package_qty,
           :category,:dosage_form,:maker,:manufacturer,:seller,:yj_code,:discontinued_on,:last_lot_expiry,:updated_on)""",
        records,
    )
    built_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "source_date": medhot_date,
        "medhot_date": medhot_date,
        "hot_date": hot_date or "",
        "built_at": built_at,
        "record_count": str(stats["records"]),
        "attribution": ATTRIBUTION,
        "source_medhot": MEDHOT_PAGE,
        "source_hot": HOT_PAGE,
    }
    con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    con.commit()
    con.execute("VACUUM")
    con.close()

    gz_path = out_dir / "drugmaster.sqlite.gz"
    with open(db_path, "rb") as f, gzip.open(gz_path, "wb", compresslevel=9) as g:
        g.write(f.read())
    sha = hashlib.sha256(gz_path.read_bytes()).hexdigest()
    sha_db = hashlib.sha256(db_path.read_bytes()).hexdigest()
    result = {
        **meta,
        "record_count": stats["records"],
        "level_counts": {"dispense": stats["level_1"], "sales": stats["level_2"], "case": stats["level_3"]},
        "dup_gtin": stats["dup_gtin"],
        "bad_check_digit": stats["bad_check_digit"],
        "db_bytes": db_path.stat().st_size,
        "gz_bytes": gz_path.stat().st_size,
        "sha256_gz": sha,
        "sha256_db": sha_db,
    }
    (out_dir / "build-info.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"db={result['db_bytes']} gz={result['gz_bytes']} sha256_gz={sha[:12]}…")
    return result


def verify_db(db_path: Path, min_records: int = 100_000) -> None:
    con = sqlite3.connect(db_path)
    meta = dict(con.execute("SELECT key, value FROM meta"))
    n = con.execute("SELECT COUNT(*) FROM drugs").fetchone()[0]
    bad_len = con.execute("SELECT COUNT(*) FROM drugs WHERE length(gtin) != 14").fetchone()[0]
    empty_name = con.execute("SELECT COUNT(*) FROM drugs WHERE brand_name = ''").fetchone()[0]
    con.close()
    problems = []
    if n < min_records:
        problems.append(f"件数が少なすぎます: {n} < {min_records}")
    if bad_len:
        problems.append(f"14 桁でない GTIN: {bad_len}")
    if empty_name:
        problems.append(f"販売名が空: {empty_name}")
    if meta.get("schema_version") != str(SCHEMA_VERSION):
        problems.append(f"schema_version 不一致: {meta.get('schema_version')}")
    if problems:
        raise SystemExit("verify 失敗:\n  " + "\n  ".join(problems))
    log(f"verify OK: records={n} source_date={meta.get('source_date')}")


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="最新版日付を取得して JSON 出力")

    b = sub.add_parser("build", help="SQLite を生成")
    b.add_argument("--out", type=Path, default=Path("dist"))
    b.add_argument("--medhot-date", help="YYYYMMDD（省略時は最新）")
    b.add_argument("--hot-date", help="YYYYMMDD（省略時は最新。'none' で HOT を使わない）")
    b.add_argument("--medhot-zip", type=Path, help="ローカル ZIP を使う（テスト用）")
    b.add_argument("--hot-zip", type=Path, help="ローカル ZIP を使う（テスト用）")

    v = sub.add_parser("verify", help="DB を検査")
    v.add_argument("--db", type=Path, required=True)
    v.add_argument("--min-records", type=int, default=100_000)

    a = p.parse_args(argv)

    if a.cmd == "check":
        print(json.dumps(latest_dates(), ensure_ascii=False))
        return 0

    if a.cmd == "build":
        dates = {}
        if not (a.medhot_date and a.hot_date):
            dates = latest_dates()
        medhot_date = a.medhot_date or dates["medhot_date"]
        hot_date = None if a.hot_date == "none" else (a.hot_date or dates["hot_date"])
        if a.medhot_zip:
            medhot_zip = a.medhot_zip.read_bytes()
        else:
            log(f"download {MEDHOT_ZIP.format(date=medhot_date)}")
            medhot_zip = fetch(MEDHOT_ZIP.format(date=medhot_date))
        hot_zip = None
        if hot_date:
            if a.hot_zip:
                hot_zip = a.hot_zip.read_bytes()
            else:
                log(f"download {HOT_ZIP.format(date=hot_date)}")
                hot_zip = fetch(HOT_ZIP.format(date=hot_date))
        info = build_db(medhot_zip, hot_zip, medhot_date, hot_date, a.out)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "verify":
        verify_db(a.db, a.min_records)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
