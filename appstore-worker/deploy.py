#!/usr/bin/env python3
"""把应用商店 Worker 部署到 Cloudflare。

不依赖 wrangler —— 全程走 REST API，只要账号邮箱 + Global API Key。

    export CF_EMAIL=you@example.com
    export CF_KEY=xxxxxxxx
    python3 deploy.py --host store.sanhe.dpdns.org

做的事：
  1. 找到账号与 zone
  2. 建（或复用）KV namespace，灌入 seed/catalog.json
  3. 上传 Worker 脚本，绑定 KV 与 STORE_KEY
  4. 绑定自定义域名

重复执行是幂等的：namespace 按 title 找，Worker 用 PUT 覆盖。
"""
import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid

API = "https://api.cloudflare.com/client/v4"
HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_NAME = "pocketpanel-store"
KV_TITLE = "pocketpanel_store"
CATALOG_KEY = "catalog:v1"


def die(msg):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


class CF:
    def __init__(self, email, key):
        self.email = email
        self.key = key

    def call(self, method, path, body=None, ctype="application/json", raw=None):
        url = API + path
        data = raw
        if data is None and body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Auth-Email", self.email)
        req.add_header("X-Auth-Key", self.key)
        if data is not None:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw_err)
            except ValueError:
                return {"success": False, "errors": [{"message": raw_err[:300]}]}


def ok(res, what):
    if not res.get("success"):
        errs = "; ".join(str(e.get("message")) for e in res.get("errors") or [])
        die(f"{what} 失败：{errs or res}")
    return res.get("result")


def pick_account(cf, wanted=""):
    accs = ok(cf.call("GET", "/accounts?per_page=50"), "读取账号") or []
    if not accs:
        die("这个账号下没有可用的 account")
    if wanted:
        for a in accs:
            if a["id"] == wanted or a["name"] == wanted:
                return a
        die(f"找不到账号 {wanted}，可选：{[a['name'] for a in accs]}")
    if len(accs) > 1:
        print(f"  账号有 {len(accs)} 个，用第一个：{accs[0]['name']}（可用 --account 指定）")
    return accs[0]


def pick_zone(cf, host):
    """从 hostname 往上找 zone：store.sanhe.dpdns.org → sanhe.dpdns.org → dpdns.org"""
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        cand = ".".join(parts[i:])
        res = cf.call("GET", f"/zones?name={cand}")
        zones = res.get("result") or []
        if zones:
            return zones[0]
    return None


def ensure_kv(cf, aid):
    res = cf.call("GET", f"/accounts/{aid}/storage/kv/namespaces?per_page=100")
    for ns in res.get("result") or []:
        if ns.get("title") == KV_TITLE:
            print(f"  KV 已存在：{ns['id']}")
            return ns["id"]
    ns = ok(cf.call("POST", f"/accounts/{aid}/storage/kv/namespaces",
                    {"title": KV_TITLE}), "创建 KV")
    print(f"  KV 已创建：{ns['id']}")
    return ns["id"]


def put_kv(cf, aid, ns_id, key, text):
    url = f"{API}/accounts/{aid}/storage/kv/namespaces/{ns_id}/values/{key}"
    req = urllib.request.Request(url, data=text.encode(), method="PUT")
    req.add_header("X-Auth-Email", cf.email)
    req.add_header("X-Auth-Key", cf.key)
    req.add_header("Content-Type", "text/plain")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        die(f"写 KV 失败：{e.read().decode('utf-8', 'replace')[:300]}")


def multipart(fields, files):
    """手搓 multipart/form-data，Cloudflare 的 Worker 上传要它。"""
    boundary = "----pp" + uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{k}"\r\n\r\n{v}\r\n').encode()
    for k, (fn, content, ctype) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{k}"; filename="{fn}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def upload_worker(cf, aid, script_path, ns_id):
    src = open(script_path, "rb").read()
    meta = {
        "main_module": "index.js",
        "compatibility_date": "2026-10-30",
        "bindings": [
            {"type": "kv_namespace", "name": "APP_STORE", "namespace_id": ns_id},
        ],
    }
    body, ctype = multipart(
        {"metadata": json.dumps(meta)},
        {"index.js": ("index.js", src, "application/javascript+module")},
    )
    res = cf.call("PUT", f"/accounts/{aid}/workers/scripts/{WORKER_NAME}",
                  raw=body, ctype=ctype)
    ok(res, "上传 Worker")
    print(f"  Worker 已上传：{WORKER_NAME}")


def put_secret(cf, aid, name, value):
    """secrets 接口吃 JSON，不吃 multipart（和脚本上传不一样）。"""
    res = cf.call("PUT", f"/accounts/{aid}/workers/scripts/{WORKER_NAME}/secrets",
                  {"name": name, "text": value, "type": "secret_text"})
    ok(res, f"设置密钥 {name}")
    print(f"  密钥 {name} 已设置")


def bind_domain(cf, aid, zone, host):
    """Global API Key 调不动 workers/domains（Custom Domain 只认 API Token），
    所以走「DNS 占位记录 + Worker Route」这条路，效果一样。"""
    if not zone:
        print("  ! 没找到 zone，跳过自定义域名。请先在 Cloudflare 添加该域名。")
        return
    zid = zone["id"]

    # 1) 记录：Worker 路由要求 hostname 有 proxied 记录，100:: 是官方占位地址
    #    只建 AAAA 不够 —— 纯 IPv4 网络里解析不到名字，必须同时给一条 A。
    recs = cf.call("GET", f"/zones/{zid}/dns_records?name={host}").get("result") or []
    have = {r["type"] for r in recs}
    for rtype, content in (("A", "192.0.2.1"), ("AAAA", "100::")):
        if rtype in have:
            print(f"  DNS {rtype} 记录已存在")
            continue
        out = cf.call("POST", f"/zones/{zid}/dns_records", {
            "type": rtype, "name": host, "content": content, "proxied": True,
            "ttl": 1, "comment": "PocketPanel app store",
        })
        if not out.get("success"):
            errs = "; ".join(str(e.get("message")) for e in out.get("errors") or [])
            print(f"  ! 建 {rtype} 记录失败：{errs}")
            continue
        print(f"  DNS 记录已建：{rtype} {host} → {content}（proxied）")

    # 2) 路由：host/* → worker
    pattern = f"{host}/*"
    routes = cf.call("GET", f"/zones/{zid}/workers/routes").get("result") or []
    for r in routes:
        if r.get("pattern") == pattern:
            if r.get("script") != WORKER_NAME:
                out = cf.call("PUT", f"/zones/{zid}/workers/routes/{r['id']}",
                              {"pattern": pattern, "script": WORKER_NAME})
                ok(out, "更新路由")
                print(f"  路由已改指：{pattern} → {WORKER_NAME}")
            else:
                print(f"  路由已存在：{pattern}")
            return
    out = cf.call("POST", f"/zones/{zid}/workers/routes",
                  {"pattern": pattern, "script": WORKER_NAME})
    if not out.get("success"):
        errs = "; ".join(str(e.get("message")) for e in out.get("errors") or [])
        print(f"  ! 建路由失败：{errs}")
        return
    print(f"  路由已建：{pattern} → {WORKER_NAME}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="store.sanhe.dpdns.org", help="Worker 的自定义域名")
    ap.add_argument("--account", default="", help="account id 或名称（多账号时指定）")
    ap.add_argument("--store-key", default="", help="客户端访问密钥，空则生成")
    ap.add_argument("--admin-key", default="", help="写入清单用的管理员密钥")
    args = ap.parse_args()

    email = os.environ.get("CF_EMAIL", "").strip()
    key = os.environ.get("CF_KEY", "").strip()
    if not email or not key:
        die("需要 CF_EMAIL 与 CF_KEY 环境变量")

    cf = CF(email, key)
    print("→ 校验凭据")
    acc = pick_account(cf, args.account)
    aid = acc["id"]
    print(f"  账号：{acc['name']}（{aid[:8]}…）")

    print("→ 找 zone")
    zone = pick_zone(cf, args.host)
    print(f"  zone：{zone['name'] if zone else '未找到'}")

    print("→ 准备 KV")
    ns_id = ensure_kv(cf, aid)
    seed = open(os.path.join(HERE, "seed", "catalog.json"), encoding="utf-8").read()
    put_kv(cf, aid, ns_id, CATALOG_KEY, seed)
    print(f"  已写入 {len(json.loads(seed)['apps'])} 个应用")

    print("→ 上传 Worker")
    upload_worker(cf, aid, os.path.join(HERE, "src", "index.js"), ns_id)

    store_key = args.store_key or os.environ.get("STORE_KEY", "") or uuid.uuid4().hex
    print("→ 设置密钥")
    put_secret(cf, aid, "STORE_KEY", store_key)
    admin_key = args.admin_key or os.environ.get("ADMIN_KEY", "") or uuid.uuid4().hex
    put_secret(cf, aid, "ADMIN_KEY", admin_key)

    print("→ 绑定域名")
    bind_domain(cf, aid, zone, args.host)

    print("\n完成。")
    print(f"  商店地址：https://{args.host}")
    print(f"  访问密钥：{store_key}")
    print(f"  管理密钥：{admin_key}")
    print("\n把地址和访问密钥填到面板「应用 → 商店」里即可。")


if __name__ == "__main__":
    main()
