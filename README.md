# 抖音火花定时发送

在电脑上扫码，选好友、时间和发送内容，然后把配置放到 Linux 服务器。服务器按北京时间发送；电脑和手机不用一直开着。文字、普通表情和抖音聊天里的原生表情都能选。

## 准备

- 一台能打开抖音网页聊天的电脑，装有 Python 3.10 或更新版本。
- 一台使用 systemd 的 Linux 服务器，装有 Python 3.10+、`python3-venv` 和 `sudo`。以下命令按 Ubuntu 22.04/24.04 写。
- 服务器能访问 `douyin.com`。想从公网打开管理页，还需要一个 HTTPS 反向代理；只用 SSH 转发则不需要。

安装前先把这个仓库下载到电脑。在项目目录运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python setup_web.py
```

设置页会在本机打开，抖音扫码窗口也会弹出。首次使用可以给本机页面设密码，也可以跳过。扫码后回到设置页，勾选好友，设置北京时间和内容，点“保存设置”。好友或表情没刷新出来时，点“更新好友与表情”。

保存后会生成 `config.json` 和 `auth.json`。先做一次只定位、不发送的检查：

```bash
.venv/bin/python spark.py --dry-run
```

## 装到服务器

下面把 `user@server` 换成自己的 SSH 用户名和地址，把 GitHub 地址换成实际仓库地址。先登录服务器，装好 Python，并把仓库克隆到自己的目录：

```bash
ssh user@server
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
git clone https://github.com/USERNAME/douyin-spark.git ~/douyin-spark
exit
```

从电脑上传刚才生成的两个文件：

```bash
scp config.json auth.json user@server:~/douyin-spark/
```

重新登录服务器执行：

```bash
ssh user@server
cd ~/douyin-spark
bash deploy/install.sh
```

脚本会安装 Chromium 和运行库、检查登录与好友定位、设置每天的发送任务，并启动管理网页。第一次安装会提示给服务器管理页设密码，账号固定为 `admin`。本机设置页如果选了“跳过密码”，这里仍需设一个：服务器网页会常驻运行。

安装完成后核对状态：

```bash
systemctl is-active douyin-spark.timer douyin-spark-web.service douyin-spark-reschedule.path douyin-spark-check.timer
systemctl list-timers 'douyin-spark*' --no-pager
```

这四项应显示 `active`。定时器列表里的时间可能按服务器时区显示，配置里的发送时间始终按北京时间解释。安装脚本只做定位检查，不会立即群发。

## 打开管理页

先用 SSH 转发试一下。在电脑上另开一个终端：

```bash
ssh -L 59177:127.0.0.1:59177 user@server
```

保持这个终端连接，浏览器打开 `http://127.0.0.1:59177/`，用 `admin` 和安装时设的密码登录。这里能改好友、时间、内容，也能绑定 ntfy。修改时间后，网页会等服务器定时器更新完成再提示保存成功。

要从公网访问，在现有 HTTPS 站点的 Caddyfile 中加一条路由。Caddy 和程序都运行在宿主机时：

```caddyfile
redir /douyin /douyin/ 308
handle_path /douyin/* {
    reverse_proxy 127.0.0.1:59177
}
```

宿主机安装的 Caddy 可以这样检查和重载：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

地址是 `https://你的域名/douyin/`。Caddy 如果在 Docker 容器里，`127.0.0.1` 指的是容器本身；先找出容器能访问的宿主机私有地址，再用它安装网页服务，例如：

```bash
SPARK_WEB_HOST=172.21.0.1 bash deploy/install.sh
```

同时把 Caddy 的 `reverse_proxy` 地址改成同一个私有地址。这个 IP 只是示例，要按自己的网络填写。管理网页的 59177 端口无需开放给公网。已有站点的其他路由不用改。

## 通知和日常操作

在管理页的“ntfy 通知”里填 `https://ntfy.sh/频道名`，点“保存并测试”；留空会生成一个频道。手机装 ntfy，订阅同一个地址。登录失效、发送失败和发送成功时会推送。频道地址保存在服务器的 `data/notify_url`。

查看发送日志：

```bash
journalctl -u douyin-spark.service -n 80 --no-pager
```

忘记管理页密码时，SSH 登录服务器，在项目目录执行下面的命令重设。旧的网页登录会失效：

```bash
.venv/bin/python setup_web.py --set-admin-password
```

抖音会话失效时，在电脑上重新运行 `setup_web.py` 扫码，保存后只把新的 `auth.json` 上传到服务器，再重启管理页：

```bash
scp auth.json user@server:~/douyin-spark/auth.json
ssh user@server 'sudo systemctl restart douyin-spark-web.service'
```

定时任务下次运行也会读取新的登录状态。每天发送前还有一次登录检查，失败会走 ntfy 通知。

## 发到 GitHub

仓库里放源码、`requirements.txt`、`deploy/` 和这份 README。`auth.json` 是抖音登录状态，`config.json` 有好友和消息内容，`data/` 有管理密码哈希、通知频道和发送记录；这些文件都在 `.gitignore` 里。提交前跑一次 `git status --short`，确认它们没进暂存区。
