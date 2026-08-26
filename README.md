# 📧 LARK-MAIL 邮件监控推送

[![Python](https://img.shields.io/badge/Python-3.7+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker%20Compose-✓-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 监控邮箱收件箱,每当收到新邮件时,自动通过飞书自定义机器人推送通知到飞书群。
> 基于 IMAP 协议轮询,一条命令 Docker 部署,开箱即用。

## ✨ 功能特性

- 📬 **IMAP 轮询监控**:定时检查邮箱未读邮件(默认每 60 秒一次)
- 📨 **飞书卡片推送**:以交互卡片形式推送通知,包含发件人、主题、时间、正文预览(前 500 字符)
- 🛡️ **防封号处理**:模拟 Outlook 客户端发送 IMAP `ID` 命令,规避网易等邮箱的 "Unsafe Login" 拦截
- ⏱️ **防限流机制**:每封邮件之间可配置延时,避免触发飞书 Webhook 频率限制
- 🔕 **旧邮件过滤**:跳过指定时间之前的邮件(默认 24 小时),避免历史邮件刷屏
- ⚖️ **批量控制**:单次最多处理 N 封(默认 50 封),防止积压时一次性推送过多
- 💬 **降级容错**:卡片消息发送失败时自动回退为纯文本消息重试
- 🔄 **失败自愈**:主循环异常时自动捕获,持续运行不退出

## 🔧 工作原理

```
┌────────────┐   IMAP/SSL(993)   ┌────────────┐   HTTPS POST   ┌──────────────┐
│  邮箱服务器 │ ◄──────────────► │ lark-mail  │ ─────────────► │  飞书群机器人  │
│ (如163邮箱) │   轮询未读邮件     │   脚本      │  卡片消息      │  (Webhook)   │
└────────────┘                   └────────────┘                └──────────────┘
```

每次轮询执行以下流程:

1. 连接 IMAP 服务器,发送 `ID` 命令模拟 Outlook 客户端
2. 登录并搜索 `INBOX` 中的未读邮件(`UNSEEN`)
3. 按数量限制取前 N 封,逐封拉取内容并**标记为已读**
4. 解析邮件:解码 MIME 头(主题/发件人)、提取正文(优先纯文本,回退 HTML)、解析日期(转换为北京时间)
5. 过滤:邮件日期早于 `BLOCK_BEFORE` 秒的旧邮件直接跳过
6. 推送:调用飞书 Webhook 发送卡片消息,失败则回退纯文本
7. 每封邮件之间休眠 `BATCH_DELAY` 秒,防止限流
8. 全部处理完后休眠 `POLL_INTERVAL` 秒,进入下一轮

## 📁 目录结构

```
LARK-MAIL/
├── lark-mail.py        # 主脚本:IMAP 监控 + 飞书推送
├── docker-compose.yml  # Docker Compose 部署配置
├── .env.example        # 环境变量示例(复制为 .env 后填写)
└── README.md           # 本文档
```

## 🚀 快速开始

### 方式一:Docker Compose 部署(推荐)

1. **准备邮箱授权码**

   以 163 邮箱为例:登录网页版邮箱 → 设置 → POP3/SMTP/IMAP → 开启 IMAP 服务,获取**授权码**。注意:授权码不是邮箱登录密码。

2. **创建飞书机器人 Webhook**

   在飞书群 → 设置 → 群机器人 → 添加「自定义机器人」,复制 Webhook 地址。

3. **配置环境变量**

   ```bash
   cp .env.example .env
   ```

   编辑 `.env`,填入你的邮箱与飞书配置:

   ```bash
   IMAP_SERVER=imap.163.com
   IMAP_USER=your_email@163.com
   IMAP_PASS=your_auth_code          # 授权码,不是登录密码!
   FEISHU_WEBHOOKS=https://open.feishu.cn/open-apis/bot/v2/hook/your_webhook
   ```

   > `.env` 已被 .gitignore 忽略,不会提交到仓库,请放心填写真实配置。

4. **启动服务**

   ```bash
   docker compose up -d
   ```

5. **查看日志**

   ```bash
   docker logs -f lark-mail
   ```

### 方式二:直接运行(无需 Docker)

```bash
# 安装依赖
pip install requests

# 配置环境变量后运行
export IMAP_SERVER=imap.163.com
export IMAP_USER=your_email@163.com
export IMAP_PASS=your_auth_code
export FEISHU_WEBHOOKS=https://open.feishu.cn/open-apis/bot/v2/hook/xxx

python lark-mail.py
```

## ⚙️ 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `IMAP_SERVER` | 是 | `imap.163.com` | IMAP 服务器地址 |
| `IMAP_PORT` | 否 | `993` | IMAP SSL 端口 |
| `IMAP_USER` | 是 | - | 邮箱账号 |
| `IMAP_PASS` | 是 | - | **授权码**,不是邮箱登录密码 |
| `MAILBOX` | 否 | `INBOX` | 监听的邮箱文件夹 |
| `FEISHU_WEBHOOKS` | 是 | - | 飞书自定义机器人 Webhook 地址 |
| `POLL_INTERVAL` | 否 | `60` | 轮询间隔(秒) |
| `BLOCK_BEFORE` | 否 | `86400` | 跳过多少秒之前的邮件(默认 24 小时) |
| `MAX_EMAILS_PER_RUN` | 否 | `50` | 每次轮询最多处理的邮件数 |
| `BATCH_DELAY` | 否 | `2.0` | 每封邮件之间的延时(秒),防止飞书限流 |
| `DEBUG` | 否 | `false` | 设为 `true` 开启调试模式,输出 IMAP 交互详情 |

## ❓ 常见问题

### 1. 登录报错 `Unsafe Login` / 登录失败

- 确认 `IMAP_PASS` 填的是**授权码**而非登录密码
- 163/QQ 等邮箱需要在网页端开启 IMAP 服务
- 脚本已内置 `ID` 命令模拟 Outlook 客户端,若仍有问题可在 `DEBUG=true` 下查看详细日志

### 2. 收不到飞书通知

- 检查 Webhook 地址是否正确(注意脚本读取的环境变量名为 `FEISHU_WEBHOOKS`,带 S)
- 确认飞书机器人未被移出群聊、Webhook 未被关闭
- 查看容器日志 `docker logs -f lark-mail`,确认是"没有新邮件"还是推送失败

### 3. 邮件被标记为已读

脚本拉取未读邮件后会自动打上 `\Seen` 标记,这是设计行为——避免同一封邮件被重复推送。若希望保留未读状态,可修改 `lark-mail.py` 中 `imap_fetch_unseen` 函数末尾的 `mail.uid('store', uid, '+FLAGS', '\\Seen')` 一行。

### 4. 历史邮件刷屏

脚本默认跳过 24 小时前的邮件。如果积压了大量未读邮件,可调小 `BLOCK_BEFORE`(如 `3600` = 只推最近 1 小时),并调小 `MAX_EMAILS_PER_RUN` 控制单次推送量。

### 5. 其他邮箱(QQ / Gmail / Outlook)

只需修改 `IMAP_SERVER` / `IMAP_PORT` / `IMAP_USER` / `IMAP_PASS` 即可适配其他邮箱服务商,授权码获取方式参见对应邮箱的设置页面。Gmail 建议使用应用专用密码。

## 🤝 贡献

欢迎提 Issue 和 Pull Request!

1. Fork 本仓库
2. 创建功能分支:`git checkout -b feature/xxx`
3. 提交修改:`git commit -m 'feat: xxx'`
4. 推送到分支:`git push origin feature/xxx`
5. 提交 Pull Request

## ⚠️ 注意事项

- 脚本为**轮询模式**,并非实时推送,通知延迟取决于 `POLL_INTERVAL`
- 飞书自定义机器人 Webhook 有限流策略,如遇到「请求过快」报错,可适当增大 `BATCH_DELAY`
- Webhook 地址与邮箱授权码属于敏感信息,请勿提交到公开仓库(`.env` 已在 .gitignore 中排除)
- 生产环境建议关闭 `DEBUG`,避免敏感信息(如邮箱凭据)出现在日志中

## 📄 许可证

[MIT](LICENSE) © 2026 LARK-MAIL Contributors
