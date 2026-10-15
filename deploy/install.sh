#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
project_dir="$(pwd)"
if [[ ! -f config.json || ! -f auth.json ]]; then
  echo '缺少 config.json 或 auth.json；先在电脑上运行 setup_web.py，保存后上传这两个文件。' >&2
  exit 1
fi
if ! command -v systemctl >/dev/null; then
  echo '服务器需要 systemd。' >&2
  exit 1
fi
if ! command -v sudo >/dev/null; then
  echo '安装 systemd 服务需要 sudo。' >&2
  exit 1
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("需要 Python 3.10 或更新版本")
PY

web_host="${SPARK_WEB_HOST:-127.0.0.1}"
web_port="${SPARK_WEB_PORT:-59177}"
python3 - "$web_host" "$web_port" <<'PY'
import ipaddress
import sys

try:
    host = ipaddress.IPv4Address(sys.argv[1])
    port = int(sys.argv[2])
except ValueError as exc:
    raise SystemExit(f"网页监听地址或端口无效：{exc}")
if host.is_unspecified or not (host.is_loopback or host.is_private) or not 1 <= port <= 65535:
    raise SystemExit("网页只能监听本机或私有 IPv4 地址，端口范围 1–65535")
PY

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
if command -v apt-get >/dev/null; then
  sudo .venv/bin/python -m playwright install-deps chromium
fi
.venv/bin/python -m playwright install chromium
mkdir -p data
chmod 700 data
chmod 600 config.json auth.json

echo '先检查登录状态和好友定位……'
.venv/bin/python spark.py --dry-run

if [[ "$(.venv/bin/python -c 'from setup_web import admin_mode; print(admin_mode())')" != password ]]; then
  if [[ ! -t 0 ]]; then
    echo '服务器网页需要管理密码。请在 SSH 交互终端运行 .venv/bin/python setup_web.py --set-admin-password，然后重跑安装脚本。' >&2
    exit 1
  fi
  echo '给服务器管理网页设置密码；账号是 admin。'
  .venv/bin/python setup_web.py --set-admin-password
fi

send_time="$(.venv/bin/python -c 'from spark import load_config; print(load_config()["send_time"])')"
current_user="$(id -un)"
system_python="$(command -v python3)"
timer_time="*-*-* ${send_time}:00 Asia/Shanghai"
systemd-analyze calendar "$timer_time" >/dev/null

sudo tee /etc/systemd/system/douyin-spark.service >/dev/null <<EOF
[Unit]
Description=Douyin spark daily chat sender
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$current_user
WorkingDirectory=$project_dir
Environment=HOME=$HOME
ExecStart=$project_dir/.venv/bin/python $project_dir/spark.py
TimeoutStartSec=600
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
EOF

sudo tee /etc/systemd/system/douyin-spark.timer >/dev/null <<EOF
[Unit]
Description=Run Douyin spark sender daily at $send_time Beijing time

[Timer]
OnCalendar=$timer_time
Persistent=false
AccuracySec=1min
Unit=douyin-spark.service

[Install]
WantedBy=timers.target
EOF

sudo install -d -m 755 /usr/local/libexec /etc/systemd/system/douyin-spark.timer.d
sudo install -m 755 deploy/reschedule.py /usr/local/libexec/douyin-spark-reschedule
sudo tee /etc/systemd/system/douyin-spark-reschedule.service >/dev/null <<EOF
[Unit]
Description=Apply Douyin spark send time from config

[Service]
Type=oneshot
ExecStart=$system_python /usr/local/libexec/douyin-spark-reschedule $project_dir/config.json $project_dir/data/schedule_status.json
NoNewPrivileges=true
PrivateTmp=true
EOF

sudo tee /etc/systemd/system/douyin-spark-reschedule.path >/dev/null <<EOF
[Unit]
Description=Watch Douyin spark settings for send time changes

[Path]
PathChanged=$project_dir/config.json
Unit=douyin-spark-reschedule.service

[Install]
WantedBy=multi-user.target
EOF

if [[ -s data/notify_url ]]; then
  chmod 600 data/notify_url
fi
sudo tee /etc/systemd/system/douyin-spark-alert@.service >/dev/null <<EOF
[Unit]
Description=Notify about failed Douyin spark %i task

[Service]
Type=oneshot
User=$current_user
WorkingDirectory=$project_dir
Environment=HOME=$HOME
ExecStart=$project_dir/.venv/bin/python $project_dir/notify.py %i
TimeoutStartSec=45
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
EOF

sudo mkdir -p /etc/systemd/system/douyin-spark.service.d
sudo tee /etc/systemd/system/douyin-spark.service.d/notify.conf >/dev/null <<EOF
[Unit]
OnFailure=douyin-spark-alert@daily.service
EOF

sudo tee /etc/systemd/system/douyin-spark-check.service >/dev/null <<EOF
[Unit]
Description=Check Douyin login before scheduled send
After=network-online.target
Wants=network-online.target
OnFailure=douyin-spark-alert@check.service

[Service]
Type=oneshot
User=$current_user
WorkingDirectory=$project_dir
Environment=HOME=$HOME
ExecStart=$project_dir/.venv/bin/python $project_dir/spark.py --dry-run
TimeoutStartSec=180
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
EOF

sudo tee /etc/systemd/system/douyin-spark-check.timer >/dev/null <<EOF
[Unit]
Description=Check Douyin login before sending

[Timer]
OnCalendar=*-*-* 02:45:00 Asia/Shanghai
Persistent=false
AccuracySec=1min
Unit=douyin-spark-check.service

[Install]
WantedBy=timers.target
EOF

sudo tee /etc/systemd/system/douyin-spark-web.service >/dev/null <<EOF
[Unit]
Description=Douyin spark settings page
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$current_user
WorkingDirectory=$project_dir
Environment=HOME=$HOME
ExecStart=$project_dir/.venv/bin/python -u $project_dir/setup_web.py --reuse-auth --no-open --serve --listen-host $web_host --port $web_port
Restart=on-failure
RestartSec=5
TimeoutStartSec=90
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now douyin-spark.timer
sudo systemctl enable --now douyin-spark-reschedule.path
sudo systemctl start douyin-spark-reschedule.service
sudo systemctl enable --now douyin-spark-check.timer
sudo systemctl enable --now douyin-spark-web.service
echo "已设置北京时间每天 $send_time 执行。"
echo "管理网页监听 ${web_host}:${web_port}。"
systemctl list-timers douyin-spark.timer --no-pager
