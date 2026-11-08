#!/usr/bin/env python3
"""多类型任务体系 HTTP 验收：注册表、动态表单数据、结构化结果、field_errors。"""
import os, time, json, subprocess
import requests

BASE = os.environ.get("PP_BASE", "http://127.0.0.1:8099")
PW = os.environ.get("PP_PASS", "REDACTED-PASSWORD")
ok, fails = 0, []


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""))
    if cond:
        ok += 1
    else:
        fails.append(name)


def dev_exec(s, cmd, timeout=40):
    """在面板所在机器上执行命令（测试需要碰设备文件系统）。"""
    t = s.post(BASE + "/api/exec", json={"cmd": cmd}).json()["id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = s.get(BASE + f"/api/exec/{t}").json()
        if d.get("status") != "running":
            return d.get("output", "")
        time.sleep(0.5)
    return ""


SELF_PORT = int(os.environ.get("PP_SELF_PORT", "8888"))
SELF = f"http://127.0.0.1:{SELF_PORT}"


def wait_run(s, tid, trigger="manual", timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = s.get(BASE + f"/api/schedule/{tid}/runs").json().get("runs", [])
        for r in runs:
            if r["trigger"] == trigger:
                return r
        time.sleep(1.5)
    return None


def main():
    s = requests.Session()
    assert s.post(BASE + "/api/login", json={"username": "admin", "password": PW}).ok
    for t in s.get(BASE + "/api/schedule").json().get("tasks", []):
        s.post(BASE + f"/api/schedule/{t['id']}/delete")

    # 注册表
    types = s.get(BASE + "/api/tasktypes").json()["types"]
    ids = [t["type_id"] for t in types]
    check("注册表包含四类", set("shell service log_clean url_visit".split()) <= set(ids), ids)
    lc = [t for t in types if t["type_id"] == "log_clean"][0]
    check("log_clean 支持 dry-run", lc["supports_dry_run"] is True)
    uv = [t for t in types if t["type_id"] == "url_visit"][0]
    body_f = [f for f in uv["fields"] if f["name"] == "body"][0]
    check("请求体条件显示元数据", body_f.get("show_when", {}).get("not_in") == ["GET", "HEAD"])
    check("multiselect 选项来自注册项",
          [o["value"] for o in lc["fields"][0]["options"]] ==
          ["panel_logs", "task_logs", "site_logs", "system_logs", "backups", "pkg_cache"])

    # 旧模型兼容：只传 cmd
    r = s.post(BASE + "/api/schedule", json={"name": "旧式任务", "expr": "0 5 * * *", "cmd": "true"})
    check("旧模型自动映射 shell", r.ok and r.json()["type"] == "shell" and
          r.json()["params"]["cmd"] == "true", r.text[:120])
    old_tid = r.json()["id"]

    # field_errors 定位到字段
    r = s.post(BASE + "/api/schedule", json={"name": "bad", "type": "url_visit",
               "expr": "*/5 * * * *", "params": {"url": "ftp://x", "method": "GET"}})
    fe = r.json().get("field_errors", {})
    check("字段级错误可定位", r.status_code == 400 and "url" in fe, r.text[:160])
    r = s.post(BASE + "/api/schedule", json={"name": "bad2", "type": "log_clean",
               "expr": "*/5 * * * *", "params": {"targets": ["nope"], "keep_policy": "days", "keep_days": 7}})
    check("multiselect 非法项被拒", "targets" in r.json().get("field_errors", {}), r.text[:160])

    # dry-run 端点
    r = s.post(BASE + "/api/tasktype/log_clean/dryrun",
               json={"params": {"targets": ["panel_logs", "task_logs", "pkg_cache"],
                                "keep_policy": "days", "keep_days": 1, "dry_run": True}})
    d = r.json()
    check("dryrun 端点返回结构化统计", r.ok and "total" in d and "targets" in d, r.text[:120])
    check("dryrun 不落历史",
          all(x["trigger"] != "dry-run" for x in s.get(BASE + "/api/schedule").json()["runs"]))

    # url_visit 全链路：打面板自身端点（对被测面板而言 127.0.0.1 一定可达）
    r = s.post(BASE + "/api/schedule", json={
        "name": "拨测", "type": "url_visit", "expr": "*/10 * * * *",
        "params": {"url": SELF + "/api/health", "method": "GET",
                   "expect_keyword": '"ok"', "save_body": True}})
    check("创建 url_visit 任务", r.ok and "访问 http" in r.json()["human"], r.text[:160])
    check("列表 human 拼接周期", r.json()["human"].startswith("每 10 分钟 访问"), r.json()["human"])
    tid = r.json()["id"]
    s.post(BASE + f"/api/schedule/{tid}/run")
    run = wait_run(s, tid)
    check("结构化结果落历史", run and run["type"] == "url_visit" and run["result"], run)
    check("成功判定+摘要", run and run["result"]["ok"]
          and '"ok"' in run["result"]["body_snippet"], run)
    check("状态码/耗时在结果里", run and run["result"]["status"] == 200 and "duration_ms" in run["result"])

    # 失败 → 状态码不符 + 消息中心
    before = s.get(BASE + "/api/messages").json()["unread"]
    r = s.post(BASE + "/api/schedule", json={
        "name": "拨测404", "type": "url_visit", "expr": "*/10 * * * *",
        "params": {"url": SELF + "/api/notexist", "method": "GET", "timeout": 3}})
    tid404 = r.json()["id"]
    s.post(BASE + f"/api/schedule/{tid404}/run")
    run404 = wait_run(s, tid404)
    check("404 判定为失败", run404 and not run404["result"]["ok"] and
          run404["result"]["fail"]["code"] == "status_mismatch", run404)
    time.sleep(0.5)
    check("失败触发通知", s.get(BASE + "/api/messages").json()["unread"] > before)

    # 连接失败（重试）
    r = s.post(BASE + "/api/schedule", json={
        "name": "拨测拒连", "type": "url_visit", "expr": "*/10 * * * *",
        "params": {"url": "http://127.0.0.1:1/", "method": "GET", "timeout": 2,
                   "retries": 1, "retry_interval": 1}})
    tidnet = r.json()["id"]
    s.post(BASE + f"/api/schedule/{tidnet}/run")
    runnet = wait_run(s, tidnet, timeout=25)
    check("连接失败+重试", runnet and runnet["result"]["fail"]["code"] == "network"
          and runnet["result"]["retries_used"] == 1, runnet)

    # log_clean 全链路：在设备上造一个 9 天前的归档 → dry-run → 实际执行
    fake = "~/pocketpanel/data/logs/@40000000ui-test.s"
    fake_abs = "~/pocketpanel/data/logs/@40000000ui-test.s"
    out = dev_exec(s, (
        "mkdir -p ~/pocketpanel/data/logs && python3 -c \""
        "import os,time; p=os.path.expanduser('~')+'/pocketpanel/data/logs/@40000000ui-test.s'; "
        "open(p,'w').write('z'*2048); t=time.time()-9*86400; os.utime(p,(t,t)); print('made')\""))
    check("设备侧造旧日志", "made" in out, out[:120])
    r = s.post(BASE + "/api/schedule", json={
        "name": "清日志", "type": "log_clean", "expr": "0 4 * * *",
        "params": {"targets": ["panel_logs"], "keep_policy": "days", "keep_days": 7,
                   "dry_run": True, "max_mb": 500, "max_files": 1000}})
    check("创建 log_clean", r.ok and "清理面板日志" in r.json()["human"], r.text[:160])
    check("human 带保留策略与预览标记", "保留 7 天" in r.json()["human"] and "预览" in r.json()["human"],
          r.json()["human"])
    tidlc = r.json()["id"]
    s.post(BASE + f"/api/schedule/{tidlc}/run")
    runlc = wait_run(s, tidlc)
    check("dry-run 结构化结果", runlc and runlc["result"]["dry_run"] is True and
          runlc["result"]["targets"]["panel_logs"]["files"] == 1, runlc)
    check("dry-run 未删除", "EXISTS" in dev_exec(
        s, f"test -f {fake} && echo EXISTS || echo GONE"))

    # 关闭 dry-run 再跑 → 文件消失
    s.post(BASE + f"/api/schedule/{tidlc}", json={
        "name": "清日志", "type": "log_clean", "expr": "0 4 * * *",
        "params": {"targets": ["panel_logs"], "keep_policy": "days", "keep_days": 7,
                   "dry_run": False, "max_mb": 500, "max_files": 1000}})
    s.post(BASE + f"/api/schedule/{tidlc}/run")
    runlc2 = wait_run(s, tidlc, trigger="manual", timeout=20)
    # wait_run 只匹配 trigger，拿到的可能还是旧 dry-run 记录；重查最新
    runs = s.get(BASE + f"/api/schedule/{tidlc}/runs").json()["runs"]
    runlc2 = [x for x in runs if x["result"] and not x["result"]["dry_run"]][0]
    check("实际执行与 dry-run 一致", runlc2["result"]["targets"]["panel_logs"]["files"] == 1)
    check("文件已被删除", "GONE" in dev_exec(
        s, f"test -f {fake} && echo EXISTS || echo GONE"))

    # service 类型：动态选项 + 保护
    svcs = [t for t in types if t["type_id"] == "service"][0]["fields"][0]["options"]
    r = s.post(BASE + "/api/schedule", json={
        "name": "重启面板", "type": "service", "expr": "0 5 * * *",
        "params": {"service": "pocketpanel-py", "action": "restart"}})
    if r.ok:
        check("service 任务创建（真机有该服务）", True)
        s.post(BASE + f"/api/schedule/{r.json()['id']}/delete")
    else:
        check("面板自身重启被保护/服务不存在", r.status_code in (400, 404, 409), r.text[:120])

    # shell 兼容
    s.post(BASE + f"/api/schedule/{old_tid}/run")
    rold = wait_run(s, old_tid)
    check("迁移后的 shell 正常执行", rold and rold["type"] == "shell" and rold["status"] == "success")

    # 清理
    for t in s.get(BASE + "/api/schedule").json().get("tasks", []):
        s.post(BASE + f"/api/schedule/{t['id']}/delete")
    s.post(BASE + "/api/messages/clear")

    print(f"\n{ok} 项通过，{len(fails)} 项失败")
    if fails:
        print("失败项：", fails)


if __name__ == "__main__":
    main()
