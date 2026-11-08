# 安全说明

这份文档说明 mailstack 默认做了哪些防护、信任边界在哪、以及上线前建议补做的事项。

## 1. 组件与进程隔离

| 组件 | 运行身份 | 监听 |
|---|---|---|
| `maddy` | 系统用户 `maddy` | 0.0.0.0:25 / 465 / 587 / 993（需 `CAP_NET_BIND_SERVICE`） |
| `maddy-admin` | `maddy` | **仅** 127.0.0.1:8080 |
| `nginx` | `www-data` | 0.0.0.0:80 / 443 |

管理台从不直接暴露公网：nginx 终止 TLS 反代 `/admin/`，并附加安全响应头。
systemd 单元启用 `ProtectSystem=strict`、`ProtectHome=true`、`NoNewPrivileges=true`，
写权限仅限邮件目录、运行目录与密钥目录。

## 2. 传输与身份

- **TLS**：仅 TLS 1.2/1.3；明文 IMAP（143）不开启；587 强制 STARTTLS；465/993 隐式 TLS
- **证书**：Let's Encrypt，`--deploy-hook` 自动同步到 Maddy 与 nginx，90 天自动续期
- **反伪造**：SPF `-all`、DKIM 2048 位、DMARC `p=quarantine`（严格对齐）、MTA-STS `enforce`、TLS-RPT
- **非开放中继**：25 端口只接受投递给本域；本域发信必须走 465/587 且通过认证；
  `authorize_sender` 校验发件人与登录身份一致

## 3. Web 管理台

| 面 | 措施 |
|---|---|
| 会话 | 服务端内存会话，随机 32 字节 SID；Cookie `HttpOnly` + `Secure` + `SameSite=Lax`；2 小时空闲 / 12 小时绝对超时 |
| 认证 | 用真实 IMAP `LOGIN` 校验密码，不落库明文；管理员后台代操作时始终以配置的邮箱身份执行 |
| 授权 | 普通用户后端强制 `account == session.account`，忽略前端传入的账号参数 |
| CSRF | 双提交令牌（页面注入 + `X-CSRF-Token`），`hmac.compare_digest` 比较 |
| 来源校验 | `Origin`/`Referer` 白名单（本域主机名、IP），缺失时放行非浏览器客户端 |
| 暴力破解 | 按 IP 与账号双维度计数，5 次失败锁定 10 分钟；注册按 IP 限速 |
| 输入 | 邮箱正则限定本域；密码 10–128 位、至少 3 类字符；文件名/查询参数全部白名单化 |
| 输出 | 所有用户数据 `html.escape`；邮件 HTML 经服务端清洗后放入 `iframe sandbox`（无 `allow-scripts`）渲染 |
| 附件 | 通过 IMAP 序号读取，不拼接文件系统路径，天然无路径穿越 |
| 审计 | 登录、失败、改密、删除、组织变更写入 `/var/lib/maddy/admin/auth.log` |
| 超管查阅 | 仅 `ADMIN_EMAIL` 可开抽屉查阅他人邮箱；只读（不发 `\Seen`），每次打开写 `AUDIT_MAILBOX`（操作者/目标/IP/时间，不含主题与正文） |

## 4. 密钥与文件权限

```
/etc/maddy-admin/            0750 maddy:maddy
  auth.env                   0600   管理台兼容密码
  mailpass                   0600   管理员邮箱密码
  register.env               0600   注册邀请码
/var/lib/maddy/              0750 maddy:maddy
  credentials.db                       bcrypt 哈希
  dkim_keys/*.key            0600
/etc/mailstack/install.conf 0600   含 DNS API 凭据（如填写）
/etc/maddy/certs/<host>/privkey.pem 0600
```

## 5. 已知边界

1. **单域实例**：一个实例服务一个主域，多域请多实例。
2. **无内置 2FA**：管理员账号建议使用长随机密码，并限制 `/admin/` 的来源 IP（nginx `allow/deny`）。
3. **会话在内存**：重启管理台会导致所有人重新登录；多实例部署不共享会话。
4. **无内置反垃圾引擎**：仅做 SPF/DKIM/DMARC 校验与基础限速，不做内容评分。
   需要更强过滤可前置 Rspamd（未集成）。
5. **未内置 fail2ban 规则**：安装脚本不擅自改防火墙；建议自行启用 fail2ban 并针对
   `maddy` 日志的认证失败做 jail。
6. **IP 证书未纳入**：仅域名证书；直接用 IP 访问 HTTPS 会证书不匹配（属预期）。

## 6. 上线加固清单

- [ ] 25 端口出站解封后，确认没有被云厂商标记为"垃圾邮件高风险"
- [ ] 配置 PTR 反向解析（提升送达率）
- [ ] 管理员密码 16 位以上，保存在密码管理器
- [ ] `/admin/` 增加来源限制（nginx `allow <办公网>; deny all;`）或置于 VPN 后
- [ ] 启用 fail2ban：SSH + Maddy 认证失败
- [ ] 开启自动安全更新（`unattended-upgrades`）
- [ ] 配置异地备份，验证过一次恢复流程
- [ ] 观察 1–2 周 DMARC 报告后，将 `p=quarantine` 升级为 `p=reject`
- [ ] 关闭不需要的注册入口（保持 `REGISTER_OPEN=false`）

## 7. 漏洞报告

请勿公开提交 Issue。发送邮件到仓库维护者邮箱，说明版本、复现步骤与影响，
48 小时内回复。修复发布后会在 CHANGELOG 中致谢（如你愿意署名）。
