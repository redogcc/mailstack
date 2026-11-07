# 安装手册

本文是 [README](../README.md) 的展开版：每一步做了什么、参数怎么填、出错怎么查。

## 1. 前置条件

| 项目 | 要求 | 检查命令 |
|---|---|---|
| 系统 | Ubuntu 22.04/24.04、Debian 12，systemd | `cat /etc/os-release` |
| 权限 | root | `id -u` 返回 0 |
| 网络 | 公网 IPv4，25 端口出站放行 | `curl -s ifconfig.me`、`nc -zv 163mx01.mxmail.netease.com 25` |
| 端口 | 25/80/443/465/587/993 未被占用 | `ss -tlnp \| grep -E ':(25\|80\|443\|465\|587\|993)'` |
| 域名 | `mail.<domain>` A 记录指向本机 | `dig +short A mail.<domain>` |

> **25 端口出站**是自建邮件最常见的坑。多数云厂商默认封禁，
> 需要在控制台提交"解封 25 端口"工单。未解封时收信正常、发信会卡在队列里。

## 2. 交互安装

```bash
git clone https://github.com/redogcc/mailstack.git
cd mailstack
sudo bash install.sh
```

向导字段说明：

| 提示 | 含义 | 建议 |
|---|---|---|
| Mail domain | 邮箱后缀 | 公司主域，例如 `acme.cn` |
| Mail hostname | 邮件服务器主机名 | 默认 `mail.acme.cn`，A 记录必须指向本机 |
| Public IPv4 | 公网 IP | 回车即自动探测；多网卡/NAT 环境请手填 |
| Administrator mailbox | 管理员邮箱 | 默认 `admin@<domain>`，同时是 Web 登录名 |
| TLS certificate | 证书获取方式 | 有 80 端口选 1；被拦/无备案选 2/3/4 |

安装过程按顺序做这些事：

1. 写 `/etc/mailstack/install.conf`（600），记录本次参数
2. `apt-get` 安装 curl/openssl/dnsutils/sqlite3/zstd/python3/nginx(/certbot)
3. 下载 Maddy 0.9.7 单二进制到 `/usr/local/bin/maddy`（已存在则复用）
4. 创建系统用户 `maddy` 与目录：`/var/lib/maddy`、`/run/maddy`、`/etc/maddy/certs/<host>`
5. 生成管理员密码，写入 `/etc/maddy-admin/{auth.env,mailpass,register.env}`
6. 申请/生成证书 → `/etc/maddy/certs/<host>/`
7. 渲染 `/etc/maddy/maddy.conf`，`verify-config` 通过后启动 `maddy.service`
8. 创建管理员邮箱（`creds create` + `imap-acct create`）
9. 安装 Web 管理台：`/opt/mailstack/admin/mailadmin.py` + `maddy-admin.service`
10. 配置 nginx（80 跳转、443 TLS、`/admin/` 反代 127.0.0.1:8080）
11. 写入 MTA-STS 策略、落地页
12. 打印 DNS 记录 → 自检 → 输出访问信息

任何一步失败都会中止并打印原因；已写入的配置会在修改前备份到
`/etc/mailstack/backups/<时间戳>/`。

## 3. 无人值守安装

```bash
cp conf/install.conf.example my.conf
$EDITOR my.conf          # 至少填 MAIL_DOMAIN、SERVER_IP
sudo bash install.sh -c my.conf -y
```

也可以全部用命令行参数覆盖：

```bash
sudo bash install.sh \
  --domain acme.cn --hostname mail.acme.cn --ip 203.0.113.10 \
  --admin admin@acme.cn --password 'Str0ng!Passw0rd' \
  --tls dns-aksign --acme-email admin@acme.cn -y
```

同一组参数可以反复执行：安装是幂等的，会备份旧配置、重建并重启服务，邮件数据不动。

## 4. 参数完整列表

见 `conf/install.conf.example`，含义与默认值：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAIL_DOMAIN` | — | 主域，必填 |
| `MAIL_HOSTNAME` | `mail.$MAIL_DOMAIN` | 必须能与 `MAIL_DOMAIN` 一起放进同一张证书 |
| `SERVER_IP` | 自动 | 用于 DNS 提示、自检、IP 访问地址 |
| `ADMIN_EMAIL` | `admin@$MAIL_DOMAIN` | 必须是本域地址 |
| `ADMIN_PASSWORD` | `auto` | 至少 10 位；`auto` 生成 18 位 |
| `TLS_MODE` | `http01` | 见下节 |
| `ACME_EMAIL` | `ADMIN_EMAIL` | Let's Encrypt 到期提醒 |
| `DNS_API_BASE` / `DNS_TOKEN` | 空 | 云厂商 DNS API 地址与 Token |
| `DNS_API_HOST` / `DNS_ACCESS_KEY` / `DNS_SECRET_KEY` | 空 | 云厂商 DNS API 主机与 AK/SK |
| `DNS_ZONE_ID` | 空 | 可留空，脚本按域名自动查 |
| `ENABLE_WEB` | `yes` | 管理台 |
| `ENABLE_LANDING` | `yes` | 落地页 |
| `WEB_PORT` | `8080` | 仅监听回环 |
| `INSTALL_PREFIX` | `/opt/mailstack` | 程序目录 |
| `STATE_DIR` | `/etc/mailstack` | 安装状态与备份 |
| `SECRETS_DIR` | `/etc/maddy-admin` | 密钥目录（600） |
| `DATA_DIR` | `/var/lib/maddy` | 邮件数据 |
| `MADDY_CONF` | `/etc/maddy/maddy.conf` | Maddy 配置 |
| `MADDY_VERSION` | `0.9.7` | 下载版本 |
| `SMTP_PORT` / `SMTPS_PORT` / `SUBMISSION_PORT` / `IMAP_PORT` | 25/465/587/993 | 特殊场景才改 |

## 5. TLS 模式详解

### http01（默认）

- 80 端口空闲 → `certbot --standalone`
- 80 端口被占用 → `certbot --webroot -w /var/www/certbot`
- 证书覆盖 `mail.<domain>`、`<domain>`、`mta-sts.<domain>`
- 续期：`certbot.timer` 自动，deploy hook 会复制证书并 reload `maddy` 与 `nginx`

### dns-manual

证书签发时脚本会打印类似：

```
Type : TXT
Name : _acme-challenge.acme.cn
Value: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

到 DNS 服务商添加后回车继续。续期需要重新执行 `mailctl cert renew` 并按提示操作；
若能拿到 API 凭据，建议改用 dns-token / dns-aksign 模式。

### dns-token / dns-aksign

脚本用 API 写入 `_acme-challenge` TXT 记录，并在**多个公共 DNS** 上确认生效后再让 CA 校验，
避免负缓存导致的随机失败。记录用完即删。

### selfsigned

自签 825 天证书，浏览器与邮件客户端会告警。仅用于本地验证流程，
例如 `--profile sandbox` 的端到端测试。

## 6. 多实例 / 隔离测试

`--profile sandbox` 会使用完全独立的路径与端口，可与生产实例共存于同一台机器：

| 项目 | 生产 | sandbox |
|---|---|---|
| 程序 | `/opt/mailstack` | `/opt/mailstack-sandbox` |
| 数据 | `/var/lib/maddy` | `/var/lib/maddy-sandbox` |
| 配置 | `/etc/maddy/maddy.conf` | `/etc/maddy/maddy-sandbox.conf` |
| 服务名 | `maddy` / `maddy-admin` | `maddy-sandbox` / `maddy-admin-sandbox` |
| 端口 | 25/465/587/993/8080 | 2525/2465/2587/2993/18080 |
| nginx | 是 | 否（管理台直连） |

```bash
sudo bash install.sh --profile sandbox --domain sandbox.test \
     --ip 203.0.113.9 --admin admin@sandbox.test --tls selfsigned -y
sudo python3 tests/e2e_sandbox.py --config /etc/mailstack-sandbox/config.json
sudo bash uninstall.sh --profile sandbox --purge -y
```

卸载脚本会校验路径必须包含 `sandbox`，避免误删生产实例。

## 7. 装完检查清单

```bash
mailctl status                  # 三个服务都 active
mailctl check                   # 全部 PASS（DNS 项在记录生效前会是 warn）
dig +short MX acme.cn           # 指向 mail.acme.cn
dig +short TXT default._domainkey.acme.cn
curl -I https://mail.acme.cn/   # 200 + HSTS
```

然后：

1. 打开 `https://mail.acme.cn/admin/`，用管理员邮箱登录
2. 「账号管理」创建第一个员工邮箱
3. 用手机/Outlook 配置 IMAP 993 + SMTP 465，确认能收发
4. 给外部邮箱（Gmail/163）发一封，检查是否进垃圾箱、查看邮件头 DKIM=pass

## 8. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `maddy` 起不来 | `journalctl -u maddy -n 50`；多为证书路径错误或端口占用 |
| 管理台 502 | `systemctl status maddy-admin`；多为 `config.json` 权限或密钥文件缺失 |
| 证书申请失败 | 80 被拦 → 换 dns 模式；DNS 未生效 → 等待后 `mailctl cert renew` |
| 发信卡队列 | 25 出站被封；`journalctl -u maddy \| grep -i queue` |
| 收不到外部信 | MX 未生效或 25 入站被安全组拦截 |
| 进垃圾箱 | SPF/DKIM/DMARC 未全通过；缺 PTR；新 IP 冷启动 |

更多见 [OPERATIONS.md](OPERATIONS.md)。
