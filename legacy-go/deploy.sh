#!/bin/sh
# 用法: HOST=<手机IP> PORT=<SSH端口> USER_TERMUX=<用户名> ./deploy.sh
set -e
HOST=${HOST:?请设置 HOST 为手机地址}
PORT=${PORT:-8022}
USER=${USER_TERMUX:-u0_a000}
REMOTE="\$HOME/pocketpanel/bin"

echo "[1/3] 交叉编译 android/arm64"
GOOS=android GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o pocketpanel .

echo "[2/3] 上传"
scp -P "$PORT" pocketpanel "$USER@$HOST:$REMOTE/pocketpanel.new"

echo "[3/3] 替换并重启"
ssh -p "$PORT" "$USER@$HOST" "cd $REMOTE && chmod +x pocketpanel.new && mv -f pocketpanel.new pocketpanel && sv restart pocketpanel && sleep 2 && sv status pocketpanel"
