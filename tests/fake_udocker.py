#!/usr/bin/env python3
"""测试用的假 udocker：只实现 udockerd 用到的那几个子命令。

真机上的 udocker 依赖 PRoot（ptrace），在 CI/容器里通常跑不起来；但
udockerd 的 API 语义、进程托管、日志、exec PTY 桥都可以用这个假客户端
完整验证。`run` 会把收到的 argv 原样写进 runs.log，测试据此断言参数拼装。
"""
import json
import os
import sys
import time
import uuid

HOME = os.environ.get("HOME", "/tmp")
REPO = os.path.join(HOME, ".udocker")
STATE = os.environ.get("FAKE_UDOCKER_STATE", os.path.join(HOME, ".fake-udocker"))
RUNS = os.path.join(STATE, "runs.log")


def _log_run(argv):
    os.makedirs(STATE, exist_ok=True)
    with open(RUNS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")


def cmd_version():
    print("version: 1.3.17-fake")
    print("tarball: fake")
    return 0


def _img_name(args):
    for a in args:
        if not a.startswith("-"):
            return a
    return ""


def cmd_pull(args):
    ref = _img_name(args[1:] if args and args[0] == "pull" else args)
    if not ref:
        print("Error: image required")
        return 1
    repo, _, tag = ref.partition(":")
    tag = tag or "latest"
    d = os.path.join(REPO, "repos", repo, tag)
    os.makedirs(d, exist_ok=True)
    cfg_digest = "sha256:" + ("%064x" % abs(hash(ref)))
    with open(os.path.join(d, "manifest"), "w", encoding="utf-8") as fh:
        json.dump({"schemaVersion": 2, "config": {"digest": cfg_digest},
                   "layers": []}, fh)
    with open(os.path.join(d, cfg_digest), "w", encoding="utf-8") as fh:
        json.dump({"architecture": "arm64", "os": "linux",
                   "created": "2025-10-01T00:00:00Z",
                   "config": {"Env": ["PATH=/usr/bin:/bin"], "Cmd": ["/bin/sh"],
                              "WorkingDir": "/"}}, fh)
    with open(os.path.join(d, "sha256:" + "a" * 64), "wb") as fh:
        fh.write(b"x" * 1024)
    print(f"Info: downloading {ref}")
    print(f"Info: layer sha256:{'a' * 8} done")
    return 0


def cmd_create(args):
    name = ""
    image = ""
    for a in args:
        if a.startswith("--name="):
            name = a.split("=", 1)[1]
        elif not a.startswith("-"):
            image = a
    if not name:
        name = uuid.uuid4().hex
    if not image:
        print("Error: image required")
        return 1
    d = os.path.join(REPO, "containers", name)
    if os.path.lexists(d):
        print(f"Error: container {name} already exists")
        return 1
    os.makedirs(os.path.join(d, "ROOT"), exist_ok=True)
    with open(os.path.join(d, "imagerepo.name"), "w") as fh:
        fh.write(image)
    print(uuid.uuid4())
    return 0


def cmd_run(args):
    _log_run(args)
    name = ""
    cmd = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--env="):
            k, _, v = a[6:].partition("=")
            os.environ[k] = v
            i += 1
            continue
        if a.startswith("--"):
            i += 1
            continue
        if not name and os.path.isdir(os.path.join(REPO, "containers", a)):
            name = a
            cmd = args[i + 1:]
            break
        i += 1
    if not name:
        print("Error: container not found")
        return 1
    print(f"fake-container {name} starting")
    sys.stdout.flush()
    if os.environ.get("FAKE_RUN_EXIT"):
        time.sleep(float(os.environ.get("FAKE_RUN_SLEEP", "0.2")))
        print("fake-container exiting with "
              + os.environ.get("FAKE_RUN_EXIT", "0"))
        return int(os.environ.get("FAKE_RUN_EXIT", "1"))
    if os.environ.get("FAKE_RUN_ECHO") == "1" and os.isatty(0):
        # 模拟交互式 shell：回显输入，exit 退出 —— 用于验证 exec 的 PTY 桥
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                if line.strip() == "exit":
                    break
                sys.stdout.write("echo:" + line)
                sys.stdout.flush()
        except (KeyboardInterrupt, OSError):
            pass
        return 0
    if os.environ.get("FAKE_RUN_LONG") == "1":
        while True:
            time.sleep(1)
    if cmd:
        print("cmd: " + " ".join(cmd))
        sys.stdout.flush()
        time.sleep(float(os.environ.get("FAKE_RUN_SLEEP", "0.2")))
        return 0
    # 常驻：等信号
    while True:
        time.sleep(1)


def cmd_rm(args):
    for a in args:
        if a.startswith("-"):
            continue
        d = os.path.join(REPO, "containers", a)
        if os.path.islink(d):
            os.unlink(d)
        elif os.path.isdir(d):
            import shutil
            shutil.rmtree(d)
        else:
            # 真 udocker 对不存在的容器就是这么说的（退出码 1 + stderr）。
            # udockerd 得认出这句话，把登记表里的幽灵记录清掉而不是报 500。
            print(f"Error: invalid container id {a}", file=sys.stderr)
            return 1
        return 0
    return 1


def cmd_rmi(args):
    ref = _img_name(args)
    if not ref:
        return 1
    repo, _, tag = ref.partition(":")
    d = os.path.join(REPO, "repos", repo, tag or "latest")
    if os.path.isdir(d):
        import shutil
        shutil.rmtree(d)
        return 0
    return 1


def cmd_load(args):
    path = ""
    for i, a in enumerate(args):
        if a in ("-i", "--input") and i + 1 < len(args):
            path = args[i + 1]
    if path and not os.path.isfile(path):
        print("Error: no such file")
        return 1
    print("Loaded image: imported:latest")
    return 0


def main():
    if len(sys.argv) < 2:
        print("usage: udocker <command>")
        return 1
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd in ("--version", "version"):
        return cmd_version()
    if cmd == "pull":
        return cmd_pull(args)
    if cmd == "create":
        return cmd_create(args)
    if cmd == "run":
        return cmd_run(args)
    if cmd in ("rm", "rmname"):
        return cmd_rm(args)
    if cmd == "rmi":
        return cmd_rmi(args)
    if cmd == "load":
        return cmd_load(args)
    if cmd == "setup":
        return 0
    if cmd == "images":
        print("REPOSITORY")
        return 0
    if cmd == "ps":
        print("CONTAINER ID")
        return 0
    print(f"Error: unsupported command {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
