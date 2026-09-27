#!/usr/bin/env python3
"""build-info.json と Release タグから、アプリが参照する manifest.json を生成する。

使い方: make_manifest.py dist/build-info.json v20260831 > manifest.json
"""
import json
import sys

REPO = "Myuta7/doublecheckun-drugmaster"


def main() -> int:
    info_path, tag = sys.argv[1], sys.argv[2]
    info = json.load(open(info_path, encoding="utf-8"))
    manifest = {
        "schema_version": int(info["schema_version"]),
        "source_date": info["source_date"],
        "medhot_date": info["medhot_date"],
        "hot_date": info["hot_date"],
        "built_at": info["built_at"],
        "record_count": info["record_count"],
        "level_counts": info["level_counts"],
        "db_url": f"https://github.com/{REPO}/releases/download/{tag}/drugmaster.sqlite.gz",
        "db_gz_bytes": info["gz_bytes"],
        "db_bytes": info["db_bytes"],
        "sha256_gz": info["sha256_gz"],
        "sha256_db": info["sha256_db"],
        "attribution": info["attribution"],
    }
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
