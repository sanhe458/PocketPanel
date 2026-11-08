#!/usr/bin/env python3.11
"""appstore.py 单元测试：类型分派 / 清单归一化 / 校验。

不起容器、不联网：docker 分支只测「能算出计划」和「明显不合法要拒绝」。
直接跑：python3 tests/test_appstore.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import appstore  # noqa: E402

OK, FAIL = 0, 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


def main():
    global OK, FAIL

    print("[1] 类型识别")
    check("缺省是 docker", appstore.type_of({"id": "x"}) == "docker")
    check("大小写归一", appstore.type_of({"type": " Docker "}) == "docker")
    check("空字符串当 docker", appstore.type_of({"type": ""}) == "docker")
    check("陌生类型原样保留", appstore.type_of({"type": "wasm"}) == "wasm")
    check("docker 有 provider", bool(appstore.provider({"type": "docker"})))
    check("termux 还没实现", appstore.provider({"type": "termux"}) is None)

    print("[2] 清单归一化")
    raw = {"updated": "2026-10-30", "apps": [
        {"id": "a1", "name": "A", "image": "x/y:1"},
        {"id": "a2", "type": "docker", "name": "B", "image": "x/y:2"},
        {"id": "a3", "type": "termux", "name": "C", "pkg": "htop"},
        {"id": "a4", "type": "wasm", "name": "D"},
        {"id": "a5", "type": "docker", "name": "缺镜像"},
        {"name": "没 id"},
        "not-a-dict",
    ]}
    out = appstore._norm(raw, "builtin")
    ids = [a["id"] for a in out["apps"]]
    check("保留 docker", "a1" in ids and "a2" in ids, ids)
    check("保留未实现类型", "a3" in ids, ids)
    check("保留陌生类型", "a4" in ids, ids)
    check("docker 缺 image 被剔除", "a5" not in ids, ids)
    check("没 id 被剔除", len(ids) == 4, ids)
    check("source 透传", out["source"] == "builtin")
    types = {t["id"]: t for t in out["types"]}
    check("docker 计数 2", types["docker"]["count"] == 2, types.get("docker"))
    check("termux 计数 1", types["termux"]["count"] == 1)
    check("docker 已实现", types["docker"]["implemented"] is True)
    check("termux 未实现且有原因", types["termux"]["implemented"] is False
          and "暂不支持" in types["termux"]["reason"])
    check("陌生类型补在列表里", "wasm" in types and types["wasm"]["count"] == 1)
    check("binary 计数 0 也列出", "binary" in types and types["binary"]["count"] == 0)

    print("[3] 安装计划分派")
    app_docker = {"id": "nginx", "type": "docker", "image": "nginx:alpine",
                  "ports": [{"container": 8080, "host": 8080}],
                  "volumes": [{"container": "/w", "host": "{{data}}/w"}],
                  "restart": "always"}
    plan = appstore.plan(app_docker, {"port": 18080, "name": "web"})
    check("端口可覆盖", plan["ports"][0]["host"] == 18080, plan["ports"])
    check("plan 带 type", plan.get("type") == "docker")
    check("数据目录落在 apps 下", plan["data_dir"].endswith("/apps/nginx"), plan["data_dir"])
    check("卷模板已展开", "{{data}}" not in plan["mounts"][0]["host"], plan["mounts"])

    try:
        appstore.plan({"id": "c", "type": "termux", "pkg": "htop"}, {})
        check("未实现类型要报错", False)
    except ValueError as e:
        check("未实现类型要报错", "暂不支持" in str(e) or "还不支持" in str(e), str(e))

    print("[4] 安装入口")
    try:
        appstore.install_task(None, "no-such-app", {})
        check("找不到应用要报错", False)
    except ValueError as e:
        check("找不到应用要报错", "没有应用" in str(e), str(e))

    print("[5] 内置清单")
    builtin = appstore._norm(appstore.BUILTIN, "builtin")
    check("内置清单有应用", len(builtin["apps"]) >= 4, len(builtin["apps"]))
    check("内置应用都标了类型", all(a.get("type") == "docker" for a in builtin["apps"]))
    check("类型视图不为空", len(builtin["types"]) >= 4)

    print("[6] 清单质量（新增应用最容易踩的坑）")
    apps = appstore.BUILTIN["apps"]
    ids = [a["id"] for a in apps]
    check("id 不重复", len(ids) == len(set(ids)))
    check("应用数量够用", len(apps) >= 10, len(apps))

    need = ("id", "name", "summary", "desc", "category", "image",
            "size", "website", "tips")
    miss = [(a.get("id"), k) for a in apps for k in need
            if not str(a.get(k) or "").strip()]
    check("必填字段齐全", not miss, miss[:4])

    # 图标名写错不会报错，界面上就是一块空白，所以对一遍 icons.js
    icons_js = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "web", "src", "icons.js")
    icons = set(re.findall(r"^\s{2}([A-Za-z0-9]+):\s*'", open(icons_js, encoding="utf-8").read(),
                           re.M))
    bad = [a["id"] for a in apps if a.get("icon_name") and a["icon_name"] not in icons]
    check("图标名都存在于 icons.js", not bad, bad)

    cats = {c for c, _ in appstore.CATS}
    bad = [a["id"] for a in apps if a.get("category") not in cats]
    check("分类都在 CATS 里", not bad, bad)

    # host 网络：端口就是宿主机端口。两个应用默认撞同一个端口，后装的必然起不来
    hosts = [p["host"] for a in apps for p in a.get("ports") or []]
    check("默认端口不重复", len(hosts) == len(set(hosts)), sorted(hosts))
    check("端口都在 1024 以上", all(1024 <= h <= 65535 for h in hosts), sorted(hosts))
    check("port_fixed 的 host 必须等于 container",
          all(p["host"] == p["container"] for a in apps if a.get("port_fixed")
              for p in a.get("ports") or []))

    bad = [(a["id"], v.get("host")) for a in apps for v in a.get("volumes") or []
           if not str(v.get("host") or "").startswith("{{data}}")]
    check("数据卷都落在 {{data}} 下", not bad, bad)

    bad = [(a["id"], e.get("key")) for a in apps for e in a.get("env") or []
           if not str(e.get("key") or "").replace("_", "").isalnum()]
    check("环境变量名合法", not bad, bad)

    noport = [a["id"] for a in apps if not a.get("ports")
              and any("{{port}}" in str(x) for x in (a.get("command") or [])
                      + [e.get("value") for e in a.get("env") or []])]
    check("没端口的应用不引用 {{port}}", not noport, noport)

    print("[7] 安装计划的变量展开")
    by_id = {a["id"]: dict(a, type="docker") for a in apps}
    rd = appstore._plan_docker(by_id["redis"], {"port": 16379})
    check("command 里的 {{port}} 会展开",
          rd["cmd"][:3] == ["redis-server", "--port", "16379"], rd["cmd"])
    vw = appstore._plan_docker(by_id["vaultwarden"], {"port": 18222})
    check("env 里的 {{port}} 会展开",
          any(e["key"] == "ROCKET_PORT" and e["value"] == "18222"
              for e in vw["env"]), vw["env"])
    ng = appstore._plan_docker(by_id["nginx"], {"port": 9999})
    check("port_fixed 忽略用户端口", ng["ports"][0]["host"] == 8080, ng["ports"])
    multi = {"id": "m", "type": "docker", "image": "x:1",
             "ports": [{"container": 3000, "host": 3000},
                       {"container": 2222, "host": 2222}]}
    pm = appstore._plan_docker(multi, {"port": 13000})
    check("多端口只改第一个", [p["host"] for p in pm["ports"]] == [13000, 2222],
          pm["ports"])
    # 入口覆盖：清单里现在没有应用用它（tini 类镜像都撤了），
    # 但能力得留着，将来遇到「入口带参数」的镜像还要靠它
    ep = appstore._plan_docker({"id": "e", "type": "docker", "image": "x:1",
                                "ports": [{"container": 8080, "host": 8080}],
                                "entrypoint": "/bin/demo"}, {})
    check("入口覆盖透传到安装计划", ep["entrypoint"] == "/bin/demo", ep["entrypoint"])

    print("[8] docker 安装任务（假 runtime）")
    import core
    import runtime

    real = (runtime.list_images, runtime.pull_task, runtime.create_container,
            core.task_log, core.task_progress, core.task_result)
    got = {}
    try:
        runtime.list_images = lambda *a, **k: []
        runtime.pull_task = lambda t, ref, plat=None: got.update(pulled=ref)
        runtime.create_container = lambda spec: (got.update(spec=spec),
                                                 {"name": spec["name"], "id": "c0ffee123456"})[1]
        core.task_log = lambda t, s: None
        core.task_progress = lambda t, n: None
        core.task_result = lambda t, r: got.update(result=r)

        app = {"id": "demo", "type": "docker", "name": "Demo", "image": "demo:1",
               "ports": [{"container": 80, "host": 8080}], "restart": "always"}
        appstore._install_docker(None, app, {"name": "d1", "port": 18081})
        check("缺镜像会先拉", got.get("pulled") == "demo:1", got.get("pulled"))
        spec = got.get("spec") or {}
        check("label 打上 pp.app", (spec.get("labels") or {}).get("pp.app") == "demo", spec.get("labels"))
        check("端口用用户填的", spec.get("ports", [{}])[0].get("host") == 18081, spec.get("ports"))
        res = got.get("result") or {}
        check("result 带 app id", res.get("app") == "demo", res.get("app"))
        check("result 带 url", res.get("urls") == ["http://127.0.0.1:18081"], res.get("urls"))
        check("没写入口时不传 Entrypoint", "entrypoint" in spec and spec["entrypoint"] is None,
              spec.get("entrypoint"))

        got.clear()
        app2 = dict(app, id="demo2", entrypoint="/bin/demo")
        appstore._install_docker(None, app2, {"name": "d2", "port": 18082})
        spec2 = got.get("spec") or {}
        check("入口覆盖进了容器 spec", spec2.get("entrypoint") == "/bin/demo",
              spec2.get("entrypoint"))
    finally:
        (runtime.list_images, runtime.pull_task, runtime.create_container,
         core.task_log, core.task_progress, core.task_result) = real
    print("[9] 与商店种子一致")
    seed_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "appstore-worker", "seed", "catalog.json")
    seed = json.load(open(seed_path, encoding="utf-8"))
    a = {x["id"]: x for x in seed.get("apps") or []}
    b = {x["id"]: dict(x, type="docker") for x in apps}
    diff = sorted(set(a) ^ set(b)) or [k for k in a if k in b and a[k] != b[k]]
    check("seed/catalog.json 与 catalog.py 同步", not diff,
          f"{diff[:4]}（改了 catalog.py 记得重新生成 seed）")
    check("seed 版本号跟上", seed.get("updated") == appstore.BUILTIN["updated"],
          (seed.get("updated"), appstore.BUILTIN["updated"]))


if __name__ == "__main__":
    main()
    print(f"\n{OK} 项通过，{FAIL} 项失败")
    sys.exit(1 if FAIL else 0)
