# 运维手册

## 1. 日常命令

```bash
mailctl status          # 服务状态与基本信息
mailctl check           # 全量自检（服务/端口/证书/DKIM/IMAP/Web/DNS）
mailctl dns             # 打印 DNS 记录
mailctl logs 200        # Maddy 最近 200 行日志
mailctl accounts        # 列出所有邮箱
mailctl add bob@acme.cn # 新建邮箱（交互输入密码）
mailctl passwd bob@acme.cn
mailctl backup          # 备份到 /var/backups/mailstack
mailctl cert info       # 证书到期时间
mailctl cert renew      # 手动续期
mailctl config          # 查看管理台读取的 config.json
```

大部分账号操作建议用 Web 管理台，`mailctl` 更适合排障和脚本化。

## 2. 备份与恢复

### 备份

```bash
sudo mailctl backup /var/backups/mailstack
# 得到 mailstack-YYYYmmdd-HHMMSS.tar.gz
```

包含：`/var/lib/maddy`（邮件、账号库、DKIM 私钥）、`install.conf`、`config.json`。
不含 TLS 证书（可重新签发）与系统包。

建议加一条 cron：

```cron
30 3 * * * root /opt/mailstack/bin/mailctl backup /var/backups/mailstack >/dev/null 2>&1
```

异地保存请自行 rsync/对象存储；备份文件含全部邮件内容，注意加密与访问控制。

### 恢复

```bash
sudo systemctl stop maddy maddy-admin
sudo tar xzf mailstack-20260101-033000.tar.gz -C /
sudo chown -R maddy:maddy /var/lib/maddy
sudo systemctl start maddy maddy-admin
```

恢复后跑一次 `mailctl check`，并用 `mailctl accounts` 核对账号数量。

## 3. 证书

| 模式 | 续期方式 |
|---|---|
| http01 / dns-token / dns-aksign | `certbot.timer` 自动，deploy hook 复制并 reload |
| dns-manual | 每 90 天执行 `mailctl cert renew`，按提示加 TXT |
| selfsigned | 825 天，到期重跑安装或手动 openssl |

检查自动化是否正常：

```bash
systemctl list-timers | grep certbot
journalctl -u certbot --since "7 days ago" | tail -20
```

`mailctl check` 里"TLS 证书"会显示到期时间，少于 7 天要处理。

## 4. 账号与组织

- Web 管理台 → 账号管理：新建 / 删除 / 行内改密 / 批量创建（≤500）
- 组织：默认六个（销售、行政、财务、技术、售后、电销），可改名、删除、新增；
  账号列表支持按组织筛选、行内下拉调整、勾选批量调整
- 激活邀请：创建后为"待激活"，用户点一次性链接（72 小时）自行设密码
- 自助注册：默认关闭，`/etc/maddy-admin/register.env` 里 `REGISTER_CODE` 为邀请码，
  `REGISTER_OPEN=true` 可开放注册（不建议）

## 5. 监控建议

最低成本方案：外部拨测 + 本机自检。

```bash
# 每分钟检查 25 端口与证书剩余天数，异常发邮件/告警
* * * * * root /opt/mailstack/bin/mailctl check --json >/dev/null || echo "mail check failed" | mail -s "[alert] mail" you@example.com
```

关注指标：

- 队列积压：`journalctl -u maddy | grep -i "queue"`，长期不降说明出站被拦或对端拒收
- 磁盘：`df -h /var/lib/maddy`，邮件只增不减，建议 70% 告警
- 证书剩余天数（`mailctl check`）
- 登录失败次数：`journalctl -u maddy-admin | grep -c LOGIN_FAILED`

## 6. 日志

```bash
journalctl -u maddy -f                 # 实时
journalctl -u maddy-admin -f
journalctl -u nginx
tail -f /var/log/nginx/access.log
```

管理台自身审计日志：`/var/lib/maddy/admin/auth.log`（登录、改密、删除等操作）。

## 7. 容量与性能

- 空账号约 40 KB（SQLite 行 + 目录），主要占用来自邮件正文和附件
- 单实例支撑数百邮箱、日均数千封信没有压力（实测 203 账号场景内存 < 40 MB）
- 附件上限：nginx `client_max_body_size 8m`；Maddy 侧按默认限制，需要更大可调整
  `maddy.conf` 的 `smtp`/`submission` 段后 `systemctl reload maddy`

## 8. 升级

```bash
cd mailstack && git pull
sudo bash install.sh -y          # 幂等：备份旧配置 → 重建 → 重启
mailctl check
```

只升级管理台而不动邮件服务：

```bash
sudo install -m 755 admin/mailadmin.py /opt/mailstack/admin/mailadmin.py
sudo systemctl restart maddy-admin
```

## 9. 迁移到新服务器

1. 旧机：`mailctl backup`，把包和 `install.conf` 拷到新机
2. 新机：改 DNS 前的低峰期，安装同一套参数（`-c install.conf -y`）
3. 新机：停服务 → 解包覆盖 `/var/lib/maddy` → 改属主 → 起服务
4. 切换 DNS A/MX 记录，观察 24 小时后再下线旧机
5. 注意 DKIM 私钥随数据一起迁移，否则已发出的签名校验会失败（新邮件不受影响）

## 10. 排障速查

| 症状 | 先看 |
|---|---|
| 发不出去 | `journalctl -u maddy -n 100`、25 出站是否被封、队列是否堆积 |
| 收不到 | MX 是否生效、安全组 25 入站、`mailctl check` |
| 管理台 502 | `systemctl status maddy-admin`、`/etc/mailstack/config.json` 权限 |
| 登录被锁 | 等 10 分钟，或 `systemctl restart maddy-admin` 清空内存计数 |
| 证书报错 | `certbot certificates`、`/etc/maddy/certs/<host>/` 文件时间 |
| 邮件进垃圾箱 | `mailctl check` 的 SPF/DKIM/DMARC、PTR、IP 信誉 |
