#!/usr/bin/env python3
"""计划任务 + 消息中心 API 验收。调度触发段要等到下一个整分钟，全程约 90 秒。"""
import base64
import os
import time

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


def wait_run(s, task_id, trigger=None, timeout=75):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = s.get(BASE + f"/api/schedule/{task_id}/runs").json().get("runs", [])
        for r in runs:
            if r["trigger"] == trigger or trigger is None:
                return r
        time.sleep(2)
    return None


def main():
    s = requests.Session()
    r = s.post(BASE + "/api/login", json={"username": "admin", "password": PW})
    check("登录", r.ok, r.text)
    # 清场
    for t in s.get(BASE + "/api/schedule").json().get("tasks", []):
        s.post(BASE + f"/api/schedule/{t['id']}/delete")

    # --- 校验 ---
    r = s.post(BASE + "/api/schedule", json={"name": "x", "expr": "61 * * * *", "cmd": "true"})
    check("分钟越界被拒", r.status_code == 400 and "分钟" in r.json().get("error", ""), r.text)
    r = s.post(BASE + "/api/schedule", json={"name": "x", "expr": "* * * *", "cmd": "true"})
    check("字段数错误被拒", r.status_code == 400, r.text)
    r = s.post(BASE + "/api/schedule", json={"name": "x", "expr": "*/5 * * * *", "cmd": ""})
    check("空命令被拒", r.status_code == 400, r.text)
    r = s.post(BASE + "/api/schedule", json={"name": "x", "expr": "0 0 31 2 *", "cmd": "true"})
    check("永假表达式可创建（警告在预览）", r.ok, r.text)
    tid_bad = r.json()["id"]
    s.post(BASE + f"/api/schedule/{tid_bad}/delete")

    # --- 创建 / 更新 ---
    r = s.post(BASE + "/api/schedule", json={
        "name": "验收-每分钟", "expr": "*/1 * * * *", "cmd": "echo SCHED_OK_44",
        "timeout": 60, "miss": "run"})
    check("创建任务", r.ok, r.text)
    tid = r.json()["id"]
    check("返回人话描述", r.json()["human"].startswith("每 1 分钟"), r.json()["human"])
    check("返回下次运行时间", isinstance(r.json().get("next_run"), int))

    r = s.post(BASE + f"/api/schedule/{tid}", json={
        "name": "验收-已改名", "expr": "*/1 * * * *", "cmd": "echo UPDATED_55",
        "timeout": 60, "miss": "skip", "enabled": True})
    check("更新任务", r.ok and r.json()["name"] == "验收-已改名", r.text)

    # --- 手动运行 ---
    s.post(BASE + f"/api/schedule/{tid}/run")
    run = wait_run(s, tid, trigger="manual", timeout=20)
    check("手动运行产生记录", run is not None)
    check("手动运行输出正确", run and "UPDATED_55" in run.get("output", ""), run)
    check("手动运行状态成功", run and run["status"] == "success")

    # --- 消息中心联动 ---
    msgs = s.get(BASE + "/api/messages").json()
    check("执行后产生消息", msgs["unread"] >= 1 and
          any("验收-已改名" in m["body"] and m["type"] == "ok" for m in msgs["items"]),
          msgs["items"][:2])
    s.post(BASE + "/api/messages/read", json={"all": True})
    check("标记已读", s.get(BASE + "/api/messages").json()["unread"] == 0)

    # --- 并发保护：cron tick 撞上仍在运行的任务 → 记 skipped ---
    tid_long = s.post(BASE + "/api/schedule", json={
        "name": "验收-长任务", "expr": "*/1 * * * *", "cmd": "sleep 40",
        "timeout": 120}).json()["id"]
    # 对齐到整分钟前 5 秒再启动，确保 40 秒任务覆盖一次分钟翻转
    wait = 60 - (time.time() % 60) - 5
    if wait > 0:
        time.sleep(wait)
    s.post(BASE + f"/api/schedule/{tid_long}/run")
    time.sleep(1)
    r2 = s.post(BASE + f"/api/schedule/{tid_long}/run")
    check("并发保护拒绝第二次运行", r2.status_code == 409, r2.text)
    print("  … 等待调度 tick 撞上运行中的任务（约 50 秒）")
    time.sleep(50)
    skipped = [x for x in s.get(BASE + f"/api/schedule/{tid_long}/runs").json().get("runs", [])
               if x["status"] == "skipped"]
    check("并发保护记 skipped", len(skipped) >= 1, len(skipped))
    wait_run(s, tid_long, trigger="manual", timeout=30)
    s.post(BASE + f"/api/schedule/{tid_long}/delete")

    # --- 调度触发（cron）---
    print("  … 等待整分钟触发（最多 75 秒）")
    run3 = wait_run(s, tid, trigger="cron", timeout=80)
    check("调度器按时触发", run3 is not None, "timeout")
    check("cron 触发输出正确", run3 and "UPDATED_55" in run3.get("output", ""), run3)

    # --- 停用后不再触发 ---
    s.post(BASE + f"/api/schedule/{tid}/toggle")
    lst = s.get(BASE + "/api/schedule").json()
    t_off = [x for x in lst["tasks"] if x["id"] == tid][0]
    check("停用后 next_run 为空", t_off["next_run"] is None and not t_off["enabled"])
    s.post(BASE + f"/api/schedule/{tid}/toggle")

    # --- @reboot ---
    r = s.post(BASE + "/api/schedule", json={"name": "开机任务", "expr": "@reboot", "cmd": "echo REBOOT_OK"})
    check("@reboot 可创建", r.ok and r.json()["expr"] == "@reboot", r.text)
    s.post(BASE + f"/api/schedule/{r.json()['id']}/delete")

    # --- 删除 ---
    s.post(BASE + f"/api/schedule/{tid}/delete")
    lst = s.get(BASE + "/api/schedule").json()
    check("删除后不在列表", all(x["id"] != tid for x in lst["tasks"]))

    # --- 清理 ---
    for t in s.get(BASE + "/api/schedule").json().get("tasks", []):
        s.post(BASE + f"/api/schedule/{t['id']}/delete")
    s.post(BASE + "/api/messages/clear")

    print(f"\n{ok} 项通过，{len(fails)} 项失败")
    if fails:
        print("失败项：", fails)


if __name__ == "__main__":
    main()
