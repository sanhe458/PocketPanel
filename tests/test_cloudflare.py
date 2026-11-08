#!/usr/bin/env python3.11
"""cloudflare.py 单元测试：域名匹配 / 错误翻译 / 凭证打码 / 端口拦截 / 存储。

不依赖真 cloudflared：安装与启动相关的分支只测「缺依赖时的报错」。
直接跑：python3 tests/test_cloudflare.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cloudflare  # noqa: E402

OK, FAIL = 0, 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {detail}")


def use_tmp_state():
    d = tempfile.mkdtemp(prefix="pp-cf-")
    cloudflare.STATE_FILE = os.path.join(d, "cloudflare.json")
    cloudflare.DIR = os.path.join(d, "cf")
    cloudflare._loaded = False
    cloudflare._state = {"creds": {}, "tunnels": []}
    cloudflare._runtime.clear()
    return d


def main():
    global OK, FAIL
    print("[1] 架构映射")
    check("aarch64 → arm64", cloudflare.arch() in ("arm64", "amd64", "arm", "386"))

    print("[2] 域名与 zone 匹配")
    zones = [{"id": "1", "name": "example.com", "status": "active"},
             {"id": "2", "name": "sub.example.com", "status": "active"},
             {"id": "3", "name": "other.net", "status": "active"}]
    check("子域命中父 zone", cloudflare._match_zone("panel.example.com", zones)["id"] == "1")
    check("最长后缀优先", cloudflare._match_zone("a.sub.example.com", zones)["id"] == "2")
    check("别的域名不误配", cloudflare._match_zone("x.other.net", zones)["id"] == "3")
    check("不匹配返回 None", cloudflare._match_zone("nope.io", zones) is None)

    print("[3] API 错误翻译")
    check("401 → 密钥无效", "密钥无效" in cloudflare._api_err({"errors": []}, 401))
    check("403 → 权限不足", "权限不足" in cloudflare._api_err({"errors": []}, 403))
    check("409 → 已存在", "已存在" in cloudflare._api_err({"errors": [{"message": "record already exists"}]}, 409))
    check("429 → 频率限制", "频率" in cloudflare._api_err({"errors": []}, 429))
    check("未知码保留原文", "boom" in cloudflare._api_err({"errors": [{"message": "boom"}]}, 500))

    print("[4] 凭证打码与掩码保护")
    use_tmp_state()
    cloudflare.save_settings(email="a@b.com", key="REALKEY", token="")
    v = cloudflare.settings_view()
    check("有凭证时 has_creds", v["has_creds"] is True)
    check("key 回显为掩码", v["key"] == "********")
    check("mode 识别 global", v["mode"] == "global")
    cloudflare.save_settings(key="********")
    check("掩码不覆盖真值", cloudflare._creds().get("key") == "REALKEY")
    cloudflare.save_settings(token="TOK")
    check("换成 token 后清掉 key", "key" not in cloudflare._creds())
    check("token 生效", cloudflare._creds().get("token") == "TOK")

    print("[5] 端口校验")
    real_cfg = cloudflare.core.cfg_get


    def fake_cfg(*keys, default=None):
        if keys and keys[0] == "init_pwd":
            return "123456"
        if keys and keys[0] == "port":
            return 8888
        return real_cfg(*keys, default=default)


    cloudflare.core.cfg_get = fake_cfg
    try:
        try:
            cloudflare._check_port(8888)
            check("面板端口 + 初始密码应拒绝", False)
        except ValueError as e:
            check("面板端口 + 初始密码应拒绝", "初始密码" in str(e))
        cloudflare._check_port(8080)
        check("普通端口放行", True)
        try:
            cloudflare._check_port(70000)
            check("越界端口应拒绝", False)
        except ValueError:
            check("越界端口应拒绝", True)
    finally:
        cloudflare.core.cfg_get = real_cfg

    print("[6] 未安装时的行为")
    use_tmp_state()
    cloudflare.BIN = "/nonexistent/cloudflared"
    cloudflare.PROOT = "/nonexistent/proot"
    cloudflare._client_cache["info"] = None
    # 显式掐掉二进制探测：否则机器上真装了 cloudflared 时，
    # _find_bin 会从 $PREFIX/bin 里把它捞出来，「未安装」这组用例就假失败
    real_find_bin = cloudflare._find_bin
    cloudflare._find_bin = lambda: ""
    try:
        info = cloudflare.client_info(force=True)
        check("找不到二进制", info["found"] is False, info)
        try:
            cloudflare.create_quick(8080)
            check("未安装创建应报错", False)
        except ValueError as e:
            check("未安装创建应报错", "还没装" in str(e))
        check("失败后不留脏实例", cloudflare.list_tunnels() == [])
    finally:
        cloudflare._find_bin = real_find_bin

    print("[7] 运行包装")
    cloudflare.IS_TERMUX = False
    argv, env = cloudflare._wrap("/usr/bin/cloudflared", ["tunnel", "--url", "http://127.0.0.1:8888"])
    check("非 Termux 不加 proot", argv[0] == "/usr/bin/cloudflared")
    check("参数原样透传", argv[1:] == ["tunnel", "--url", "http://127.0.0.1:8888"])
    check("env 是副本", env is not os.environ)
    cloudflare.IS_TERMUX = True
    cloudflare.PROOT = "/tmp/fake-proot"
    cloudflare.RESOLV = "/tmp/fake-resolv"
    open("/tmp/fake-proot", "w").close()
    open("/tmp/fake-resolv", "w").close()
    argv2, _ = cloudflare._wrap("/usr/bin/cloudflared", ["tunnel"])
    check("Termux 用 proot 绑 resolv", argv2[0] == "/tmp/fake-proot" and any("fake-resolv:/etc/resolv.conf" in x for x in argv2))
    os.remove("/tmp/fake-resolv")
    os.remove("/tmp/fake-proot")

    print("[8] 存储往返")
    use_tmp_state()
    with cloudflare._lock:
        cloudflare._state["tunnels"].append(
            {"id": "aa01", "kind": "quick", "name": "t", "port": 8080,
             "created": 1, "token": "s3cret-token", "autostart": False})
    cloudflare._save()
    check("文件权限 0600", oct(os.stat(cloudflare.STATE_FILE).st_mode & 0o777) == "0o600")
    cloudflare._loaded = False
    cloudflare._state = {"creds": {}, "tunnels": []}
    cloudflare._load()
    check("重新加载保留实例", len(cloudflare._state["tunnels"]) == 1)
    views = cloudflare.list_tunnels()
    check("列表不含 token", "token" not in views[0] and views[0]["has_token"] is True)
    check("列表含状态文案", views[0]["state_text"] in ("已停止", "启动中", "连接中", "已连接", "异常", "已停止重试"))
    try:
        cloudflare.tunnel_detail("nope")
        check("不存在的详情应 KeyError", False)
    except KeyError:
        check("不存在的详情应 KeyError", True)

    print("[9] 域名下拉缓存")
    use_tmp_state()
    real_zones = cloudflare._zones
    hits = {"n": 0}
    try:
        def fake_zones(creds=None, max_pages=3):
            hits["n"] += 1
            return [{"id": "z1", "name": "example.com", "status": "active"},
                    {"id": "z2", "name": "sanhe.dpdns.org", "status": "active"}]
        cloudflare._zones = fake_zones
        check("空缓存时 zones_view 不联网", cloudflare.zones_view()["zones"] == [] and hits["n"] == 0)
        r = cloudflare.refresh_zones()
        check("refresh 回填两个域名", len(r["zones"]) == 2 and hits["n"] == 1)
        check("settings_view 带上域名", len(cloudflare.settings_view()["zones"]) == 2)
        check("zones_view 读缓存不联网", hits["n"] == 1 and len(cloudflare.zones_view()["zones"]) == 2)
        # 重启进程后还能读到（落盘）
        cloudflare._loaded = False
        cloudflare._state = cloudflare._empty_state()
        check("落盘后重新加载仍在", len(cloudflare.zones_view()["zones"]) == 2)
        # 换凭证 → 缓存作废
        cloudflare.save_settings(token="tok-aaa")
        check("换 token 清掉域名缓存", cloudflare.zones_view()["zones"] == [])
        cloudflare._zones = fake_zones
        cloudflare.refresh_zones()
        cloudflare.save_settings(token="tok-aaa")
        check("同凭证重复保存不清缓存", len(cloudflare.zones_view()["zones"]) == 2)
    finally:
        cloudflare._zones = real_zones

    print("[10] 子域名拼装")
    cases = [("panel", "example.com", "panel.example.com"),
             ("a.b", "example.com", "a.b.example.com"),
             (".panel.", "example.com", "panel.example.com")]
    for sub, zone, want in cases:
        got = f"{cloudflare.normalize_sub(sub)}.{zone}"
        check(f"{sub!r} → {want}", got == want)
    check("大写转小写", cloudflare.normalize_sub("Panel") == "panel")
    check("空格与协议头要拒绝", cloudflare.normalize_sub("pa nel") == "")
    check("整条 URL 也能清洗", cloudflare.normalize_host("https://Panel.Example.com:8443/x?y=1") == "panel.example.com")
    check("非法 host 仍会被拒", cloudflare.normalize_host("not_a_domain") == "not_a_domain")


if __name__ == "__main__":
    main()
    print(f"\n{OK} 项通过，{FAIL} 项失败")
    sys.exit(1 if FAIL else 0)
