# 服务器管理

## 从公网打开管理页

先给服务器配置 HTTPS 站点。在站点的 Caddyfile 里加入下面两条路由，管理页就会挂在 `/douyin/`：

```caddyfile
redir /douyin /douyin/ 308
handle_path /douyin/* {
    reverse_proxy 127.0.0.1:59177
}
```

宿主机安装的 Caddy 可以这样检查并重载：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

地址是 `https://你的域名/douyin/`。Caddy 如果在 Docker 容器里，`127.0.0.1` 指向容器自己。先确定容器能访问的宿主机私有地址，再用它安装管理页，例如：

```bash
SPARK_WEB_HOST=172.21.0.1 bash deploy/install.sh
```

同时把 Caddy 的 `reverse_proxy` 改成同一个地址。上面的 IP 只是例子，按自己的网络填写。管理页的 59177 端口不用直接开放到公网。

## ntfy 通知

管理页的“通知”里填 `https://ntfy.sh/频道名`，点“保存并测试”。留空会生成一个新频道。手机安装 ntfy 并订阅同一个地址，就能收到发送成功、发送失败和登录失效的提醒。

## 查运行状态

```bash
systemctl is-active douyin-spark.timer douyin-spark-web.service douyin-spark-reschedule.path douyin-spark-check.timer douyin-spark-accounts.timer
systemctl list-timers 'douyin-spark*' --no-pager
journalctl -u douyin-spark.service -n 80 --no-pager
```

这五项应显示 `active`。定时器列表可能按服务器时区显示，设置页里的发送时间始终按北京时间保存。

## 重设管理页密码

SSH 登录服务器，在项目目录运行：

```bash
cd ~/douyin-spark
.venv/bin/python setup_web.py --set-admin-password
```

旧的网页登录会失效。

## 抖音需要重新扫码

打开管理页，选中账号，点“检查登录”。如果登录失效，点“重新登录”，用抖音扫一扫并在手机上确认。页面显示“已登录”后，好友和表情列表会更新。原账号的发送设置会保留。

添加其他抖音号时，先填一个区分账号的名称，扫码登录，再选好友、时间和内容并保存。每个账号的登录文件和发送记录分别放在 `data/accounts/` 下；原账号仍使用项目根目录的 `auth.json` 和 `config.json`。新账号可以在管理页暂停或启用定时发送。
