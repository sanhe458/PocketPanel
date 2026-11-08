#!/usr/bin/env python3
"""把面板的 catalog.py 转成商店种子 seed/catalog.json。

清单的单一真相源是 `../catalog.py`：改应用只改那里，然后跑一次这个脚本。
`tests/test_appstore.py` 会比对两边，忘了同步会直接红。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import catalog  # noqa: E402

# 固定字段顺序，diff 才看得懂
ORDER = ["id", "type", "name", "summary", "desc", "category", "icon", "icon_name",
         "image", "arch", "ports", "port_fixed", "volumes", "env", "entrypoint",
         "command", "restart", "size", "website", "tips", "verified"]


def build():
    apps = [{k: a[k] for k in ORDER if k in a}
            for a in (dict(x, type="docker") for x in catalog.APPS)]
    return {"version": 2, "updated": catalog.UPDATED, "apps": apps}


def main():
    data = build()
    path = os.path.join(HERE, "seed", "catalog.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"写入 {len(data['apps'])} 个应用 → {path}")


if __name__ == "__main__":
    main()
