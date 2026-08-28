# 📧 LARK-MAIL 邮件监控推送

[![Python](https://img.shields.io/badge/Python-3.7+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker%20Compose-✓-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 监控邮箱收件箱,每当收到新邮件时,自动通过飞书自定义机器人推送通知到飞书群。
> 基于 IMAP 协议轮询,一条命令 Docker 部署,开箱即用,支持**单账户 / 多账户**两种模式。

## ✨ 功能特性

- 📬 **IMAP 轮询监控**:定时检查邮箱未读邮件(默认每 60 秒一次)
- 📚 **多账户并行监控**:一个容器同时监控多个邮箱,每个账户独立线程运行,互不影响
- 📨 **飞书卡片推送**:以交互卡片形式推送通知,包含发件人、主题、时间、正文预览(前 500 字符)、附件名
- 🛡️ **防封号处理**:模拟 Outlook 客户端发送 IMAP `ID` 命令,规避网易等邮箱的 "Unsafe Login" 拦截
- ⏱️ **防限流机制**:每封邮件之间可配置延时(带随机抖动),避免触发飞书 Webhook 频率限制
- 🔕 **旧邮件过滤**:IMAP 搜索阶段就用 `SINCE` 条件限定最近 `BLOCK_BEFORE` 秒内的未读邮件,远古积压邮件根本不会被拉取(省流量、避免旧邮件 fetch 失败报错)
- 🚫 **关键词屏蔽**:支持按账户配置屏蔽词,命中主题或正文的邮件自动跳过,不推送
- ⚖️ **批量控制**:单次最多处理 N 封(默认 50 封),防止积压时一次性推送过多
- 💬 **降级容错**:卡片消息发送失败时自动回退为纯文本消息重试
- ✅ **推送成功才标记已读**:拉取邮件使用 `BODY.PEEK[]`,不会隐式标记已读;只有推送成功的邮件才会打上已读标记,被过滤跳过的邮件保持未读,不丢信
- 🔁 **失败重试有上限**:推送失败的邮件保持未读自动重试,超过 `MAX_RETRIES` 次(默认 3)后标记已读放弃,避免反复重试刷屏
- 🔌 **连接复用**:登录后连接持续复用,每 `RECONNECT_INTERVAL`(默认 10 分钟)才重连一次,大幅降低被邮箱服务器风控的概率;连接异常自动带退避重连(10 秒起,60 秒封顶)
- 🔄 **失败自愈**:主循环异常时自动捕获,持续运行不退出

## 🔧 工作原理

```
┌────────────┐   IMAP/SSL(993)   ┌────────────┐   HTTPS POST   ┌──────────────┐
│  邮箱服务器 │ ◄──────────────► │ lark-mail  │ ─────────────► │  飞书群机器人  │
│ (如163邮箱) │   轮询未读邮件     │   脚本      │  卡片消息      │  (Webhook)   │
└────────────┘                   └────────────┘                └──────────────┘
```

每次轮询执行以下流程:

1. 建立 IMAP 连接并登录(发送 `ID` 命令模拟 Outlook 客户端)——连接**复用**,不每轮重建
2. 搜索最近 `BLOCK_BEFORE` 秒内的未读邮件(`UNSEEN SINCE ...`,从源头排除旧邮件)
3. 使用 `BODY.PEEK[]` 按数量限制拉取前 N 封(不会隐式标记已读)
4. 解析邮件:解码 MIME 头(主题/发件人)、提取正文(优先纯文本,回退 HTML 并去除标签)、提取附件名、解析日期(转换为北京时间)
5. 双重过滤:搜索阶段已按时间限定;此处再校验邮件日期(个别邮件 Date 头异常时兜底),命中 `blocked_keywords` 屏蔽词的直接跳过——被过滤的邮件**保持未读**
6. 推送:调用飞书 Webhook 发送卡片消息(含附件信息),失败则回退纯文本
7. **推送成功**后才将该邮件标记为已读;推送失败则**保持未读**,下次轮询自动重试,超过 `MAX_RETRIES` 次后标记已读放弃
8. 每封邮件之间休眠 `BATCH_DELAY` 秒(带随机抖动),防止限流
9. 休眠 `POLL_INTERVAL` 秒后进入下一轮(连接保持复用,每 `RECONNECT_INTERVAL` 秒强制重连一次;连接异常时短退避后立即重试)

> 多账户模式下,每个账户**独立线程**并行执行上述流程,各自拥有独立的轮询间隔、屏蔽词与 Webhook。

## 📁 目录结构

```
LARK-MAIL/
├── lark-mail.py        # 主脚本:IMAP 监控 + 飞书推送
├── docker-compose.yml  # Docker Compose 部署配置(含配置注释说明)
├── .env.example        # 环境变量示例(复制为 .env 后填写,含单/多账户示例)
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

   编辑 `.env`,填入你的邮箱与飞书配置。

   > `.env` 已被 .gitignore 忽略,不会提交到仓库,请放心填写真实配置。
   > 配置方式有**单账户**和**多账户**两种,任选其一(详见下文「环境变量」章节)。
   > `docker-compose.yml` 已通过 `${VAR:-默认值}` 语法自动读取 `.env` 中的变量,
   > **日常只需改 `.env`,无需改动 yml 文件**。

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

# 配置环境变量后运行(单账户示例)
export IMAP_SERVER=imap.163.com
export IMAP_USER=your_email@163.com
export IMAP_PASS=your_auth_code
export FEISHU_WEBHOOKS=https://open.feishu.cn/open-apis/bot/v2/hook/xxx

# 多账户请改用 MAIL_ACCOUNTS_JSON(一整行,不能换行):
# export MAIL_ACCOUNTS_JSON='[{"imap_user":"user1@163.com","imap_pass":"授权码1","feishu_webhook":"https://open.feishu.cn/open-apis/bot/v2/hook/xxx","blocked_keywords":""}]'

python lark-mail.py
```

## ⚙️ 环境变量

> **变量从哪来?** Docker 部署时,`docker-compose.yml` 通过 `${VAR:-默认值}` 语法自动读取同目录 `.env` 文件:
> `.env` 中已设置 → 使用 `.env` 的值;未设置或留空 → 使用表中的默认值。**只需编辑 `.env`,无需修改 yml。**

### 单账户模式变量

| 变量名               | 必填 | 默认值         | 说明                                                         |
| -------------------- | ---- | -------------- | ------------------------------------------------------------ |
| `IMAP_SERVER`        | 是   | `imap.163.com` | IMAP 服务器地址                                              |
| `IMAP_PORT`          | 否   | `993`          | IMAP SSL 端口                                                |
| `IMAP_USER`          | 是*  | -              | 邮箱账号                                                     |
| `IMAP_PASS`          | 是*  | -              | **授权码**,不是邮箱登录密码                                  |
| `MAILBOX`            | 否   | `INBOX`        | 监听的邮箱文件夹                                             |
| `FEISHU_WEBHOOKS`    | 是*  | -              | 飞书自定义机器人 Webhook 地址                                |
| `BLOCKED_KEYWORDS`   | 否   | 空             | 屏蔽词,逗号分隔,命中主题/正文则跳过                          |
| `POLL_INTERVAL`      | 否   | `60`           | 轮询间隔(秒)                                                 |
| `BLOCK_BEFORE`       | 否   | `86400`        | 跳过多少秒之前的邮件(默认 24 小时)                           |
| `MAX_EMAILS_PER_RUN` | 否   | `50`           | 每次轮询最多处理的邮件数                                     |
| `MAX_RETRIES`        | 否   | `3`            | 推送失败最大重试次数,超限后标记已读放弃(避免反复重试)        |
| `RECONNECT_INTERVAL` | 否   | `600`          | 连接复用下强制重连间隔(秒),默认 10 分钟重连一次,降低被邮箱风控概率 |
| `BATCH_DELAY`        | 否   | `2.0`          | 每封邮件之间的延时(秒),防止飞书限流                          |
| `DEBUG`              | 否   | `false`        | 设为 `true` 开启调试模式,输出 IMAP 交互详情与每轮检查日志(默认静默,只有新邮件/异常/重连才打日志) |

### 多账户模式变量(推荐)

| 变量名               | 必填 | 说明                                                     |
| -------------------- | ---- | -------------------------------------------------------- |
| `MAIL_ACCOUNTS_JSON` | 是*  | 多账户 JSON 数组配置,设置后**优先**使用,单账户变量被忽略 |

> `*` 表示多账户模式(`MAIL_ACCOUNTS_JSON`)与单账户变量(`IMAP_USER` / `IMAP_PASS` / `FEISHU_WEBHOOKS`)二选一。

**JSON 格式说明:**

```json
[
  {
    "imap_user": "user1@163.com",               // 邮箱账号(必填)
    "imap_pass": "授权码1",                      // 授权码,不是登录密码(必填)
    "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",  // 飞书 Webhook(必填)
    "blocked_keywords": "广告,推广"              // 屏蔽词,逗号分隔(可选)
  },
  {
    "imap_user": "user2@qq.com",
    "imap_pass": "授权码2",
    "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/yyy",
    "blocked_keywords": ""
  }
]
```

**实际填写时(`.env` 或 docker-compose.yml 中)必须写成一行:**

```bash
MAIL_ACCOUNTS_JSON=[{"imap_user":"user1@163.com","imap_pass":"授权码1","feishu_webhook":"https://open.feishu.cn/open-apis/bot/v2/hook/xxx","blocked_keywords":"广告,推广"},{"imap_user":"user2@qq.com","imap_pass":"授权码2","feishu_webhook":"https://open.feishu.cn/open-apis/bot/v2/hook/yyy","blocked_keywords":""}]
```

> ⚠️ **多账户 JSON 不能换行!**
>
> - 必须写成**一整行**,即使只有一个账户也不例外
> - `.env` 或 docker-compose.yml 中换行,会把值拆成多行,脚本 `json.loads` 解析失败,服务直接报错无法启动
> - 账户之间用 `,` 分隔,整个数组用 `[` `]` 包裹,键名和值都要用**双引号**

## ❓ 常见问题

### 1. 登录报错 `Unsafe Login` / `LOGIN Login error` / 登录失败

- 确认 `IMAP_PASS` / `imap_pass` 填的是**授权码**而非登录密码
- 163/QQ 等邮箱需要在网页端开启 IMAP 服务
- 脚本已内置 `ID` 命令模拟 Outlook 客户端,若仍有问题可在 `DEBUG=true` 下查看详细日志
- 若 `LOGIN Login error` 表现为**时好时坏**(有时正常推送、有时连不上):多为邮箱服务器对高频重连的间歇风控。新版已改为**连接复用**(每 `RECONNECT_INTERVAL` 才重连一次)+ **失败退避**(10 秒起、60 秒封顶自动重试),升级代码并重启容器后通常自动缓解
- 确认授权码无误且连接稳定后仍持续失败,可尝试用浏览器登录一次网页版邮箱"激活"账号

### 2. 收不到飞书通知

- 检查 Webhook 地址是否正确(注意脚本读取的环境变量名为 `FEISHU_WEBHOOKS`,带 S)
- 确认飞书机器人未被移出群聊、Webhook 未被关闭
- 查看容器日志 `docker logs -f lark-mail`,确认是"没有新邮件"还是推送失败
- 检查是否被 `blocked_keywords` 屏蔽词误过滤

### 3. 邮件什么时候会被标记为已读?

脚本**只在推送成功后**才将邮件标记为已读:

- 被时间过滤 / 屏蔽词跳过的邮件 → **保持未读**
- 单封拉取(fetch)失败的邮件 → **保持未读**,下轮自动重试;连续失败超过 `MAX_RETRIES` 次 → **标记已读放弃**,避免死循环
- 推送失败(卡片和纯文本都失败)的邮件 → **保持未读**,下次轮询自动重试,避免丢信
- 推送失败超过 `MAX_RETRIES` 次(默认 3) → **标记已读放弃**,避免 webhook 配错时反复重试刷屏
- 同一轮**连续多封** fetch 失败 → 视为连接异常,自动重连并退避,不再逐个空跑
- 推送成功的邮件 → 打上 `\Seen` 标记,避免同一封邮件被重复推送

> 拉取邮件使用 `BODY.PEEK[]`,不会像 `RFC822`/`BODY[]` 那样隐式标记已读,保证上述"推送成功才标记"的策略真实生效。

若希望所有邮件都保留未读状态,可删除 `lark-mail.py` 中 `monitor_account` 函数里的 `mail.uid('store', uid, '+FLAGS', '\\Seen')` 一行(约第 386/395 行)。

### 4. 历史邮件刷屏

脚本默认跳过 24 小时前的邮件。如果积压了大量未读邮件,可调小 `BLOCK_BEFORE`(如 `3600` = 只推最近 1 小时),并调小 `MAX_EMAILS_PER_RUN` 控制单次推送量。

### 5. 其他邮箱(QQ / Gmail / Outlook)

只需修改 `IMAP_SERVER` / `IMAP_PORT` / `IMAP_USER` / `IMAP_PASS` 即可适配其他邮箱服务商,授权码获取方式参见对应邮箱的设置页面。Gmail 建议使用应用专用密码。多账户模式下,每个账户可配置不同的 `imap_server` / `imap_port`(在 JSON 中加上这两个字段即可)。

### 6. 多账户 JSON 解析报错(`解析 MAIL_ACCOUNTS_JSON 失败`)

- 确认 `MAIL_ACCOUNTS_JSON` 是**一整行**,中间没有任何换行
- 每个账户用 `{ }` 包裹,账户之间用 `,` 分隔,整体是 `[ ]` 数组
- 键名和值都用**双引号**,检查是否漏了引号或括号
- 可在本地先验证 JSON 合法性:`python -c "import json,sys; json.loads(sys.argv[1])" '你的JSON'`,无输出即合法

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
- **`MAIL_ACCOUNTS_JSON` 多账户 JSON 必须写成一整行**,换行会导致解析失败、服务无法启动
- 设置 `MAIL_ACCOUNTS_JSON` 后,单账户变量(`IMAP_USER` 等)会被忽略
- Webhook 地址与邮箱授权码属于敏感信息,请勿提交到公开仓库(`.env` 已在 .gitignore 中排除)
- 生产环境建议关闭 `DEBUG`,避免敏感信息(如邮箱凭据)出现在日志中

## 📄 许可证

[MIT](LICENSE) © 2026 LARK-MAIL Contributors
