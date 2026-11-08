"""指标采集：CPU、内存、磁盘、电池、服务、进程、网络、日志。

Android 10 的限制与 Go 版一致：读不到 /proc/stat、/sys/class/thermal，
因此 CPU 靠 top 的进程行汇总，温度与电池靠 Termux:API。
"""
import json
import os
import platform
import re
import socket
import threading
import time

import core
from core import SVC_DIR, PREFIX, sh

# ---------------- CPU ----------------

def quota_cpus():
    """进程实际可用的核数。Android 的 cpuset 会限制，os.cpu_count() 读不到这层限制。"""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 2


CPU = {"val": 0.0, "cores": os.cpu_count() or 2, "procs": 0, "running": 0}
CPU_LOCK = threading.Lock()
START_AT = time.time()


def sample_cpu():
    out = sh("top -b -n2 -d 0.2 2>/dev/null")
    lines = out.split("\n")
    cpu_line, task_line, last_hdr = "", "", -1
    for i, ln in enumerate(lines):
        t = ln.strip()
        if t.startswith("Tasks:"):
            task_line = t
        if "%cpu" in t and "%idle" in t and "PID" not in t:
            cpu_line = t
        if "PID" in t and "%CPU" in t and "ARGS" in t:
            last_hdr = i

    cores = os.cpu_count() or 2
    if cpu_line:
        for x in cpu_line.split():
            if x.endswith("%cpu"):
                try:
                    v = float(x[:-4])
                    if v >= 100:
                        cores = int(round(v / 100))
                except ValueError:
                    pass
                break

    # 汇总行在这台设备上恒为满 idle（读不到 /proc/stat），只能用进程行
    total = 0.0
    if last_hdr >= 0:
        for ln in lines[last_hdr + 1:]:
            f = ln.split()
            if len(f) < 9:
                continue
            try:
                int(f[0])
                total += float(f[8])
            except ValueError:
                continue
    val = round(total / cores, 1) if cores else 0.0
    procs, running = 0, 0
    if task_line:
        m = re.search(r"(\d+)\s+total", task_line)
        if m:
            procs = int(m.group(1))
        m = re.search(r"(\d+)\s+running", task_line)
        if m:
            running = int(m.group(1))

    with CPU_LOCK:
        CPU["val"] = min(val, 100.0)
        CPU["cores"] = cores
        if procs:
            CPU["procs"] = procs
            CPU["running"] = running


def cpu_loop():
    while True:
        try:
            sample_cpu()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(8)


# ---------------- 内存 / 磁盘 / 电池 ----------------

MEM_CACHE = {"at": 0, "used": 0, "total": 0}
DISK_CACHE = {"at": 0, "used": 0, "total": 0}
BATT_CACHE = {"at": 0, "pct": 0, "chg": False, "temp": 0.0}
DEV_CACHE = {"model": "", "android": ""}
_LOCK = threading.Lock()


def mem_info():
    used = total = 0
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0]) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
    except Exception:  # noqa: BLE001
        pass
    return used, total


def disk_usage():
    with _LOCK:
        if time.time() - DISK_CACHE["at"] < 30 and DISK_CACHE["total"]:
            return DISK_CACHE["used"], DISK_CACHE["total"]
    out = sh(f"df -k {core.HOME} 2>/dev/null | tail -1")
    f = out.split()
    if len(f) >= 4:
        try:
            total = int(f[1]) * 1024
            used = int(f[2]) * 1024
            with _LOCK:
                DISK_CACHE.update(at=time.time(), used=used, total=total)
            return used, total
        except ValueError:
            pass
    return 0, 0


def battery():
    with _LOCK:
        if time.time() - BATT_CACHE["at"] < 5 and BATT_CACHE["at"]:
            return BATT_CACHE["pct"], BATT_CACHE["chg"], BATT_CACHE["temp"]
    out = sh("termux-battery-status 2>/dev/null")
    if out:
        try:
            b = json.loads(out)
            pct = int(float(b.get("percentage", 0)))
            temp = float(b.get("temperature", 0))
            plugged = str(b.get("plugged", ""))
            chg = "PLUGGED" in plugged and "UNPLUGGED" not in plugged
            with _LOCK:
                BATT_CACHE.update(at=time.time(), pct=pct, chg=chg, temp=temp)
            return pct, chg, temp
        except Exception:  # noqa: BLE001
            pass
    return 0, False, 0.0


def device_info():
    with _LOCK:
        if DEV_CACHE["model"]:
            return DEV_CACHE["model"], DEV_CACHE["android"]
    model = sh("getprop ro.product.model")
    android = sh("getprop ro.build.version.release")
    with _LOCK:
        DEV_CACHE.update(model=model, android=android)
    return model, android


def overview():
    used, total = mem_info()
    du, dt = disk_usage()
    with CPU_LOCK:
        cpu, cores = CPU["val"], CPU["cores"]
        procs, running = CPU["procs"], CPU["running"]
    batt, chg, temp = battery()
    model, android = device_info()
    return {
        "cpu": cpu, "cores": cores, "quota": quota_cpus(),
        "mem_used": used, "mem_total": total,
        "disk_used": du, "disk_total": dt,
        "load": [0, 0, 0], "temp": temp, "battery": batt, "charging": chg,
        "uptime": round(time.time() - START_AT, 1),
        "procs": procs, "running": running, "cpu_source": "top",
        "model": model, "android": android, "arch": platform.machine(),
        "go": "Python " + platform.python_version(), "hostname": "termux",
        "time": int(time.time()),
    }


# ---------------- 服务 ----------------

def service_status(name):
    out = sh(f"SVDIR={SVC_DIR} sv status {name} 2>&1")
    st = "down"
    pid = 0
    up = ""
    if out.startswith("run:"):
        st = "run"
    elif out.startswith("fail:"):
        st = "fail"
    m = re.search(r"\(pid (\d+)\)", out)
    if m:
        pid = int(m.group(1))
    m = re.search(r"\)\s+([^;]+)", out)
    if m:
        up = m.group(1).strip()
    return {"name": name, "status": st, "pid": pid, "uptime": up, "raw": out}


def list_services():
    try:
        names = sorted(
            d for d in os.listdir(SVC_DIR)
            if os.path.isdir(os.path.join(SVC_DIR, d)) and not d.startswith(".")
        )
    except OSError:
        return []
    return [service_status(n) for n in names]


def service_action(name, action):
    if action not in ("start", "stop", "restart", "enable", "disable"):
        raise ValueError(f"不支持的操作: {action}")
    if any(c in name for c in "/\\ "):
        raise ValueError("非法服务名")
    if name == "pocketpanel" and action in ("stop", "restart"):
        raise ValueError("不能从面板内部停止自身，请在终端执行 sv restart pocketpanel")
    return sh(f"SVDIR={SVC_DIR} sv {action} {name} 2>&1")


# ---------------- 进程 ----------------

GUARD_NAMES = ("runsvdir", "runsv", "svlogd", "runit")


def proc_guard(pid, cmd, self_pid):
    if pid <= 1:
        return True, "系统进程"
    if pid == self_pid:
        return True, "面板自身"
    base = os.path.basename((cmd or " ").split(" ")[0])
    if base in GUARD_NAMES:
        return True, "runit 监督进程"
    return False, ""


def list_processes():
    out = sh("ps -A -o pid,ppid,rss,stat,args 2>/dev/null")
    me = os.getpid()
    rows = []
    for i, ln in enumerate(out.split("\n")):
        if i == 0 or not ln.strip():
            continue
        f = ln.split()
        if len(f) < 5:
            continue
        try:
            pid, ppid, rss = int(f[0]), int(f[1]), int(f[2])
        except ValueError:
            continue
        cmd = " ".join(f[4:])
        prot, reason = proc_guard(pid, cmd, me)
        rows.append({
            "pid": pid, "ppid": ppid, "rss": rss * 1024, "stat": f[3],
            "command": cmd, "protected": prot, "reason": reason,
        })
    rows.sort(key=lambda r: r["rss"], reverse=True)
    return {"list": rows, "count": len(rows), "rss_total": sum(r["rss"] for r in rows)}


def kill_process(pid, hard=False):
    me = os.getpid()
    cmd = ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode("utf-8", "replace").replace("\x00", " ").strip()
    except OSError:
        pass
    prot, reason = proc_guard(pid, cmd, me)
    if prot:
        raise PermissionError(f"受保护进程（{reason}）")
    os.kill(pid, 9 if hard else 15)


# ---------------- 网络 ----------------

PORT_HINTS = {
    22: "SSH", 80: "HTTP", 443: "HTTPS", 3000: "Node", 3306: "MySQL",
    5000: "Flask", 5432: "PostgreSQL", 6379: "Redis", 7000: "frp",
    8000: "HTTP", 8022: "sshd", 8080: "HTTP", 8081: "HTTP", 8888: "PocketPanel",
    9000: "Portainer", 9090: "Prometheus", 11211: "Memcached", 27017: "MongoDB",
    2181: "ZooKeeper", 4000: "HTTP",
}
PROBE_PORTS = [22, 80, 443, 3000, 3306, 4000, 5000, 5432, 6379,
               7000, 8000, 8022, 8080, 8081, 8888, 9000, 9090, 11211, 2181, 27017]


def parse_ifconfig():
    out = sh("ifconfig 2>/dev/null")
    items, cur = [], None
    for ln in out.split("\n"):
        if not ln.strip():
            continue
        if ln[0] not in (" ", "\t"):
            name = ln.split(":")[0].split()[0] if ln.split() else ""
            cur = {"name": name, "addr": "", "mac": "", "flags": "", "mtu": "", "addrs": []}
            m = re.search(r"flags=(\S+)", ln)
            if m:
                cur["flags"] = m.group(1)
            m = re.search(r"mtu\s+(\d+)", ln)
            if m:
                cur["mtu"] = m.group(1)
            items.append(cur)
            continue
        if cur is None:
            continue
        f = ln.split()
        if len(f) >= 2:
            if f[0] == "inet":
                cur["addr"] = f[1]
                cur["addrs"].append(f[1])
            elif f[0] == "inet6":
                cur["addrs"].append(f[1])
            elif f[0] == "ether":
                cur["mac"] = f[1]
    return [i for i in items if i["name"] and i["name"] != "lo"]


def probe_ports():
    res = []
    for port in PROBE_PORTS:
        s = socket.socket()
        s.settimeout(0.26)
        t0 = time.time()
        try:
            s.connect(("127.0.0.1", port))
            ok = True
        except OSError:
            ok = False
        finally:
            s.close()
        res.append({
            "port": port, "open": ok,
            "ms": int((time.time() - t0) * 1000),
            "hint": PORT_HINTS.get(port, ""),
        })
    return res


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def network():
    return {"ifaces": parse_ifconfig(), "ports": probe_ports(),
            "lan": lan_ip(), "probed_at": int(time.time())}


# ---------------- 日志 ----------------

LOG_DIR = os.path.join(PREFIX, "var/log/sv")


def list_logs():
    try:
        names = sorted(
            d for d in os.listdir(LOG_DIR)
            if os.path.isdir(os.path.join(LOG_DIR, d))
        )
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(LOG_DIR, n, "current")
        try:
            st = os.stat(p)
            out.append({"name": n, "size": st.st_size, "mod_time": int(st.st_mtime)})
        except OSError:
            out.append({"name": n, "size": 0, "mod_time": 0})
    return out


TAI_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}\.\d+\s")


def tail_log(name, lines=200):
    core.valid_name(name)
    path = os.path.join(LOG_DIR, name, "current")
    if not os.path.isfile(path):
        raise FileNotFoundError("日志不存在")
    max_read = 256 * 1024
    size = os.path.getsize(path)
    start = max(0, size - max_read)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read().decode("utf-8", "replace")
    rows = data.rstrip("\n").split("\n")
    rows = rows[-lines:]
    return {"name": name, "lines": [TAI_RE.sub("", r) for r in rows],
            "size": size, "truncated": start > 0}


def disk_usage_dirs():
    names = ["pocketpanel", "storage", ".termux", ".cache", ".local"]
    out = []
    for n in names:
        p = os.path.join(core.HOME, n)
        if not os.path.exists(p):
            continue
        o = sh(f"du -sk {json.dumps(p)} 2>/dev/null | tail -1")
        f = o.split()
        if f:
            try:
                out.append({"path": n, "size": int(f[0]) * 1024})
            except ValueError:
                pass
    out.sort(key=lambda x: x["size"], reverse=True)
    return out
