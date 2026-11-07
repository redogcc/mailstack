# mailstack

[![CI](https://github.com/redogcc/mailstack/actions/workflows/ci.yml/badge.svg)](https://github.com/redogcc/mailstack/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Platform](https://img.shields.io/badge/platform-Ubuntu%2022.04%20%7C%2024.04%20%7C%20Debian%2012-lightgrey)

**一条命令，把企业邮箱装进自己的服务器。**

Maddy（SMTP/IMAP）+ nginx（TLS）+ 轻量 Web 管理台，不依赖 Docker，不需要 Kubernetes，
一台 1 核 1G 的云主机就能跑起来。

[快速开始](#快速开始) · [功能](#功能) · [安装参数](#安装参数) · [日常运维](#日常运维) · [English](README.en.md)

---

## 为什么是它

| | |
|---|---|
| **一条命令** | 只问域名、公网 IP、管理员邮箱，其余自动完成 |
| **真的轻** | 实测内存：Maddy 12.5 MB + 管理台 18.5 MB + nginx 3.8 MB |
| **完整可用** | 收发信、网页邮箱、账号与组织管理、批量开通、激活邀请、行内改密 |
| **交付即安全** | TLS 1.2+、SPF/DKIM/DMARC、MTA-STS、TLS-RPT、CSRF、会话隔离、登录限速 |
| **可运维** | `mailctl` 状态/自检/备份/证书续期，配置全部模板化、可重生成 |

技术选型刻意保守：Maddy 是单二进制 MTA/IMAP 服务，配置即文本；管理台只用 Python 标准库，
没有 pip 依赖，没有 Node 构建，升级不会牵一发动全身。

---

## 快速开始

在 **Ubuntu 22.04 / 24.04 或 Debian 12** 上，以 root 运行：

```bash
git clone https://github.com/redogcc/mailstack.git
cd mailstack
sudo bash install.sh
```

向导依次询问：

```
Mail domain (the part after @) [example.com]: acme.cn
Mail hostname (A record -> this server) [mail.acme.cn]:
Public IPv4 of this server [自动探测]:
Administrator mailbox [admin@acme.cn]:
TLS certificate
  1) Let's Encrypt via HTTP-01      - 需要 80 端口可达，域名已解析
  2) Let's Encrypt via DNS-01       - 手动添加 TXT 记录，适合无 80 端口
  3) Let's Encrypt via DNS-01 API   - 用云厂商 API Token 自动完成 DNS-01
  4) Let's Encrypt via DNS-01 AK/SK - 用云厂商 AK/SK 自动完成 DNS-01
  5) self-signed                    - 仅测试用
Choose 1-5 [1]:
```

装完会打印：

1. **必须添加的 DNS 记录**（A / MX / SPF / DKIM / DMARC / MTA-STS / TLS-RPT / SRV）
2. **管理台地址**与**管理员密码**（只显示一次，同时以 600 权限保存在服务器上）
3. 自检结果（服务、端口、证书、DKIM、IMAP 登录、Web 控制台）

添加完 DNS、等待生效后，打开 `https://mail.你的域名/admin/` 就能登录。

---

## 功能

**邮件**
- SMTP（25）、Submission（587 STARTTLS / 465 TLS）、IMAPS（993）
- DKIM 2048 位自动签名、SPF/DMARC 校验、MTA-STS 强制策略、TLS-RPT
- 加号地址（`user+tag@domain`）与 `postmaster@` 别名
- 队列重试、退信、按域限速

**网页管理台**（`/admin/`）
- 登录 / 自助注册（需邀请码）/ 服务端会话 / 按账号隔离数据
- 三栏邮件客户端：文件夹、搜索、会话阅读、附件、写信/回复/转发、草稿
- 账号管理：新建、删除、行内改密、强密码生成、批量创建（最多 500）
- 组织管理：默认销售/行政/财务/技术/售后/电销，可自定义、可批量调整归属
- 激活邀请：一次性令牌 + 72 小时有效期，用户自行设置密码
- 系统自检页：DNS、证书、端口、服务、队列一览

**站点**
- 可选落地页（含登录入口），也可用 `--no-landing` 关闭
- `/admin/` 反向代理到仅监听 127.0.0.1 的管理台进程

---

## 系统要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Ubuntu 22.04 / 24.04，Debian 12（systemd） |
| CPU / 内存 | 1 vCPU / 1 GB（实测三进程合计 ~35 MB） |
| 磁盘 | 10 GB 起（邮件按需增长，`/var/lib/maddy`） |
| 网络 | 公网 IPv4；**25 端口出站必须放行**（多数云厂商需单独申请解封） |
| 域名 | 一条 A 记录指向本机；建议同时配置 PTR 反向解析 |

> 多数云厂商默认封禁 25 端口出站，需要在控制台提交工单解封，
> 否则只能收信、不能发信。

---

## 安装参数

交互式向导足够覆盖常见场景；批量/自动化部署可以用配置文件或命令行参数。

```bash
# 用配置文件（推荐：可纳入版本管理，密码留空即自动生成）
cp conf/install.conf.example my.conf && vim my.conf
sudo bash install.sh -c my.conf -y

# 或全用命令行
sudo bash install.sh --domain acme.cn --ip 203.0.113.10 \
     --tls dns-aksign --acme-email admin@acme.cn -y
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `MAIL_DOMAIN` / `--domain` | 必填 | 邮箱域名 |
| `MAIL_HOSTNAME` / `--hostname` | `mail.<domain>` | 邮件主机名，A 记录指向本机 |
| `SERVER_IP` / `--ip` | 自动探测 | 公网 IPv4，用于 DNS 记录与自检 |
| `ADMIN_EMAIL` / `--admin` | `admin@<domain>` | 管理员邮箱，同时也是 Web 登录名 |
| `ADMIN_PASSWORD` / `--password` | `auto` | `auto` 生成 18 位强密码并打印一次 |
| `TLS_MODE` / `--tls` | `http01` | `http01` / `dns-manual` / `dns-token` / `dns-aksign` / `selfsigned` |
| `DNS_API_BASE` / `DNS_TOKEN` | 空 | 云厂商 DNS API 地址 + Token（zone 读、记录写） |
| `DNS_API_HOST` / `DNS_ACCESS_KEY` / `DNS_SECRET_KEY` / `DNS_ZONE_ID` | 空 | 云厂商 DNS API 主机、AK/SK、Zone ID（可留空自动匹配） |
| `ENABLE_WEB` / `--no-web` | `yes` | 是否安装 Web 管理台 |
| `ENABLE_LANDING` / `--no-landing` | `yes` | 是否安装落地页 |
| `WEB_PORT` / `--web-port` | `8080` | 管理台监听端口（仅 127.0.0.1） |
| `SMTP_PORT` 等 | 25/465/587/993 | 仅在有前置代理等特殊场景下修改 |

其他开关：

```bash
sudo bash install.sh --dry-run        # 只渲染配置，不改动系统
sudo bash install.sh --reconfigure    # 用上次保存的参数重新生成配置
sudo bash install.sh --profile sandbox # 隔离测试实例（独立端口/目录，见 docs/INSTALL.md）
```

### TLS 证书的五种取法

| 模式 | 适用场景 | 续期 |
|---|---|---|
| `http01` | 80 端口可达、域名已解析（最常见） | `certbot.timer` 自动 |
| `dns-manual` | 无 80 端口 / 未备案被拦 / 内网 | `mailctl cert renew` 时手动加 TXT |
| `dns-token` | 云厂商提供 Bearer Token 形式的 DNS API | 自动（API 写 TXT） |
| `dns-aksign` | 云厂商提供 AK/SK 签名形式的 DNS API | 自动（API 写 TXT） |
| `selfsigned` | 本地测试 | 自签 825 天，客户端会告警 |

证书通过 `--deploy-hook` 复制到 `/etc/maddy/certs/<hostname>/` 并自动 reload 服务，
所以 Maddy 与 nginx 始终用同一份证书。

---

## 安装后

### DNS 记录

安装脚本会打印完整清单，也可以随时重看：

```bash
sudo /opt/mailstack/bin/mailctl dns
```

以 `acme.cn` 为例（`203.0.113.10` 为服务器 IP）：

| 类型 | 名称 | 值 |
|---|---|---|
| A | `mail.acme.cn` | `203.0.113.10` |
| MX | `acme.cn` | `10 mail.acme.cn` |
| TXT | `acme.cn` | `v=spf1 mx -all` |
| TXT | `default._domainkey.acme.cn` | `v=DKIM1; k=rsa; p=...`（脚本已生成） |
| TXT | `_dmarc.acme.cn` | `v=DMARC1; p=quarantine; rua=mailto:postmaster@acme.cn` |
| TXT | `_mta-sts.acme.cn` | `v=STSv1; id=...` |
| TXT | `_smtp._tls.acme.cn` | `v=TLSRPTv1; rua=mailto:postmaster@acme.cn` |
| CNAME | `mta-sts.acme.cn` | `mail.acme.cn` |
| SRV | `_submission._tcp.acme.cn` | `0 1 587 mail.acme.cn` |
| SRV | `_imaps._tcp.acme.cn` | `0 1 993 mail.acme.cn` |

细节与各服务商配置方式见 [docs/DNS.md](docs/DNS.md)。

### 客户端设置

| 协议 | 地址 | 加密 |
|---|---|---|
| IMAP | `mail.acme.cn:993` | SSL/TLS |
| SMTP | `mail.acme.cn:465` | SSL/TLS |
| SMTP | `mail.acme.cn:587` | STARTTLS |

用户名填**完整邮箱地址**。

---

## 目录结构

```
/opt/mailstack/            程序
  admin/mailadmin.py         单文件 Web 管理台（Python 标准库）
  bin/mailctl                运维命令
  scripts/                   DNS 记录、自检、ACME 钩子
/etc/mailstack/            安装状态
  install.conf               安装参数（600）
  config.json                运行时配置（管理台读取）
  backups/                   每次改动前的配置备份
/etc/maddy/maddy.conf        Maddy 主配置（模板生成）
/etc/maddy/certs/<host>/     TLS 证书（deploy hook 自动更新）
/etc/maddy-admin/           管理台密钥（auth.env / mailpass / register.env，600）
/var/lib/maddy/              邮件数据、账号库、DKIM 私钥
/var/www/landing             落地页（可选）
/var/www/mta-sts             MTA-STS 策略文件
```

---

## 日常运维

```bash
mailctl status        # 服务与概况
mailctl check         # 全量自检（DNS/证书/端口/IMAP/Web）
mailctl dns           # 打印 DNS 记录
mailctl logs 200      # Maddy 日志
mailctl accounts      # 列出邮箱
mailctl add bob@acme.cn       # 新建邮箱（交互输入密码）
mailctl passwd bob@acme.cn    # 重置密码
mailctl backup        # 打包到 /var/backups/mailstack
mailctl cert renew    # 续期证书
```

日常邮箱管理建议直接用 Web 管理台：账号、组织、批量开通、改密都在里面。
更多内容见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

---

## 安全基线

默认交付即包含：

- 仅 TLS 1.2/1.3；明文 IMAP 关闭；587 强制 STARTTLS
- 非开放中继：只允许本域认证用户发信，`source` 白名单
- SPF `-all` + DKIM 2048 + DMARC `p=quarantine`（观察后可升 `reject`）
- 管理台：HttpOnly/Secure/SameSite Cookie、CSRF 双校验、会话 2h 空闲/12h 绝对超时、
  登录失败锁定、按账号强制数据隔离
- 邮件 HTML 双层防御：服务端清洗 + `iframe sandbox` 无脚本渲染
- systemd 沙箱：`ProtectSystem=strict`、`NoNewPrivileges`、最小 `ReadWritePaths`
- 敏感文件 600；nginx 隐藏版本号并带 CSP/XFO/Referrer/Permissions 头

威胁模型与加固清单见 [docs/SECURITY.md](docs/SECURITY.md)。

---

## 升级与卸载

```bash
git pull && sudo bash install.sh -y     # 幂等重装：备份旧配置、重建、重启
sudo bash uninstall.sh                   # 移除程序，保留邮件数据
sudo bash uninstall.sh --purge           # 连邮件数据一起删除
```

---

## 常见问题

**装完能收不能发？**
云厂商封了 25 端口出站，去控制台解封（工单说明"自建邮件服务器"）。

**证书申请失败？**
`http01` 需要 80 端口真的可达、域名已解析到本机；国内未备案域名 80 会被拦，
改用 `--tls dns-manual` 或 API 模式重跑。

**邮件进垃圾箱？**
先确认 SPF/DKIM/DMARC 三条记录都生效（`mailctl check`），再考虑申请 PTR 反向解析；
新 IP 有信誉冷启动，正常通信一两周会好转。

**能不能加第二个域名？**
当前版本一个实例服务一个主域（`$(local_domains)`），多域请起多个实例（`--profile` 机制可参考）。

**数据在哪？怎么迁移？**
全部在 `/var/lib/maddy`（SQLite + 邮件文件），`mailctl backup` 打包即可搬迁。

---

## 参与

- 问题与建议：开 Issue
- 提交代码：fork + PR，请先跑 `bash tests/run_local.sh`
- 服务端端到端测试见 `tests/e2e_sandbox.py`（在服务器上运行）

## 许可

MIT，见 [LICENSE](LICENSE)。

第三方组件：Maddy（GPLv3，独立进程调用）、nginx（BSD-2-Clause）、Certbot（Apache-2.0）。
