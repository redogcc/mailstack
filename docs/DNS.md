# DNS 配置手册

邮件能不能送达，八成取决于这几条记录。安装脚本会把它们按你的域名渲染好打印出来，
本文解释每条记录的作用、为什么这样填，以及各服务商怎么操作。

## 1. 记录清单

以 `acme.cn`、服务器 IP `203.0.113.10`、主机名 `mail.acme.cn` 为例：

| # | 类型 | 名称 | 值 | 作用 |
|---|---|---|---|---|
| 1 | A | `mail.acme.cn` | `203.0.113.10` | 邮件服务器地址；所有连接都指向这里 |
| 2 | MX | `acme.cn` | `10 mail.acme.cn` | 告诉全世界"这个域的收信服务器是谁" |
| 3 | TXT | `acme.cn` | `v=spf1 mx -all` | SPF：只允许 MX 主机发信，其它一律判定伪造 |
| 4 | TXT | `default._domainkey.acme.cn` | `v=DKIM1; k=rsa; p=MIIBIj...` | DKIM 公钥（2048 位），由 Maddy 生成 |
| 5 | TXT | `_dmarc.acme.cn` | `v=DMARC1; p=quarantine; rua=mailto:postmaster@acme.cn; adkim=s; aspf=s` | DMARC：未通过校验的邮件进隔离 |
| 6 | TXT | `_mta-sts.acme.cn` | `v=STSv1; id=1720000000` | 声明存在 MTA-STS 策略，`id` 变更即触发缓存刷新 |
| 7 | TXT | `_smtp._tls.acme.cn` | `v=TLSRPTv1; rua=mailto:postmaster@acme.cn` | TLS 失败报告收集地址 |
| 8 | CNAME | `mta-sts.acme.cn` | `mail.acme.cn` | MTA-STS 策略的 HTTPS 主机 |
| 9 | SRV | `_submission._tcp.acme.cn` | `0 1 587 mail.acme.cn` | 客户端自动发现提交端口 |
| 10 | SRV | `_imaps._tcp.acme.cn` | `0 1 993 mail.acme.cn` | 客户端自动发现 IMAP 端口 |
| 11 | PTR | `203.0.113.10` → `mail.acme.cn` | 反向解析 | 可选，但对送达率影响很大（多在云厂商控制台设置） |

> DKIM 公钥不要手抄：`mailctl dns` 会直接从 `/var/lib/maddy/dkim_keys/<domain>_default.dns`
> 读取完整值。注意它通常超过 255 字符，DNS 面板里要允许长 TXT（多数服务商会自动分片）。

## 2. MTA-STS 策略文件

除了 DNS TXT，还需要一个 HTTPS 文件（安装脚本已自动写入并配置 nginx）：

```
https://mta-sts.acme.cn/.well-known/mta-sts.txt
```

内容：

```
version: STSv1
mode: enforce
mx: mail.acme.cn
max_age: 604800
```

修改 `mx` 或切换服务器后，记得同步更新 `_mta-sts` 的 `id`（任意递增数字即可），
否则对端会继续使用缓存中的旧策略。`enforce` 表示强制 TLS；刚上线可先用 `testing` 观察。

## 3. 云厂商控制台操作要点

各家控制台措辞不同，但需要填的字段是一致的：

- 记录类型选 **TXT**
- **主机记录 / Name** 填相对名：`_dmarc`、`default._domainkey`、
  `_acme-challenge`；根域 TXT 填 `@` 或留空
- **记录值 / Value** 粘贴整串，含 `v=DKIM1;` 或 `v=DMARC1;` 前缀一起复制
- 长 TXT（DKIM 公钥）如果控制台限制单条长度，按引号分段，但对外仍是同一条记录
- 若使用 CDN / 代理类产品，邮件相关记录（MX、A、TXT）务必设为
  **仅 DNS / 不代理**，否则邮件端口会被劫持

自动签发方面：

- 控制台提供 **DNS API**（Token 或 AK/SK 两种鉴权之一）时，可用
  `--tls dns-token` 或 `--tls dns-aksign` 自动写入挑战记录
- 没有 API 或不想开权限时，用 `--tls dns-manual` 手填，或用 `--tls http01`
  （需要 80 端口可达）

## 4. 生效与验证

```bash
# 权威 DNS（最快反映真实配置）
dig +short MX acme.cn @1.1.1.1
dig +short TXT acme.cn @1.1.1.1
dig +short TXT default._domainkey.acme.cn @1.1.1.1
dig +short TXT _dmarc.acme.cn @1.1.1.1

# 反向解析
dig +short -x 203.0.113.10
```

一般 1–10 分钟生效，TTL 长的话可能更久。完整自检：

```bash
mailctl check
```

输出里 DNS 相关项标 `warn` 说明记录还没生效或写错，标 `NO` 则是硬失败。

## 5. 自测送达

给一个外部邮箱发信，然后看对方邮件头：

```
Authentication-Results: mx.google.com;
       spf=pass smtp.mailfrom=acme.cn;
       dkim=pass header.d=acme.cn;
       dmarc=pass (p=QUARANTINE) header.from=acme.cn
```

三项全 `pass` 即配置正确。若 `dkim=none`，说明发信没走签名路径（检查是否用了
`sendmail` 之外的直连方式），或公钥记录没生效。

## 6. 常见错误

| 现象 | 原因 |
|---|---|
| `spf=neutral` | TXT 记录写成了 `v=spf1 mx`，缺少 `-all` |
| `dkim=permerror` | 公钥被截断（只复制了第一段） |
| `dmarc=fail` | From 域与 SPF/DKIM 对齐域不一致（`adkim=s` 要求严格对齐） |
| MX 指向 IP | MX 的值必须是主机名，不能是 IP |
| 两条 SPF 记录 | 同一名字只能有一条 SPF，多域名发送请合并到一条 |
