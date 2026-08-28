import os
import re
import time
import json
import email
import imaplib
import random
import threading
import requests
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

# ---------- 全局默认配置 ----------
BJ_TZ = timezone(timedelta(hours=8))  # 北京时间时区

DEFAULTS = {
    'imap_server': 'imap.163.com',
    'imap_port': 993,
    'mailbox': 'INBOX',
    'block_before': 86400,
    'max_emails_per_run': 50,
    'batch_delay': 2.0,
    'poll_interval': 60,
    'blocked_keywords': '',
    'max_retries': 3,                # 推送失败的最大重试次数,超限后标记已读放弃
    'reconnect_interval': 600,       # 连接复用:每 600 秒(10 分钟)强制重连一次,降低被服务器风控概率
    'debug': False,
}

def now_bj():
    """当前北京时间(日志时间戳使用,避免容器 UTC 时区差异)"""
    return datetime.now(BJ_TZ)

def load_accounts():
    """加载账户配置，优先解析 MAIL_ACCOUNTS_JSON，否则回退到单账户环境变量"""
    json_str = os.getenv('MAIL_ACCOUNTS_JSON', '').strip()
    if json_str:
        try:
            accounts = json.loads(json_str)
            if not isinstance(accounts, list):
                raise ValueError("MAIL_ACCOUNTS_JSON 必须是 JSON 数组")
            merged_accounts = []
            for acc in accounts:
                cfg = DEFAULTS.copy()
                env_map = {
                    'imap_server': 'IMAP_SERVER',
                    'imap_port': 'IMAP_PORT',
                    'mailbox': 'MAILBOX',
                    'block_before': 'BLOCK_BEFORE',
                    'max_emails_per_run': 'MAX_EMAILS_PER_RUN',
                    'batch_delay': 'BATCH_DELAY',
                    'poll_interval': 'POLL_INTERVAL',
                    'max_retries': 'MAX_RETRIES',
                    'reconnect_interval': 'RECONNECT_INTERVAL',
                    'debug': 'DEBUG',
                }
                for key, env_var in env_map.items():
                    env_val = os.getenv(env_var)
                    if env_val is not None:
                        if key == 'debug':
                            cfg[key] = env_val.lower() == 'true'
                        else:
                            cfg[key] = type(DEFAULTS[key])(env_val)
                cfg.update(acc)
                cfg['imap_port'] = int(cfg['imap_port'])
                cfg['block_before'] = int(cfg['block_before'])
                cfg['max_emails_per_run'] = int(cfg['max_emails_per_run'])
                cfg['batch_delay'] = float(cfg['batch_delay'])
                cfg['poll_interval'] = int(cfg['poll_interval'])
                cfg['max_retries'] = int(cfg['max_retries'])
                cfg['reconnect_interval'] = int(cfg['reconnect_interval'])
                cfg['debug'] = bool(cfg['debug']) if isinstance(cfg['debug'], bool) else str(cfg['debug']).lower() == 'true'
                for r in ('imap_user', 'imap_pass', 'feishu_webhook'):
                    if r not in cfg or not cfg[r]:
                        raise ValueError(f"账户配置缺少字段: {r}")
                merged_accounts.append(cfg)
            return merged_accounts
        except Exception as e:
            print(f"❌ 解析 MAIL_ACCOUNTS_JSON 失败: {e}")
            raise

    imap_user = os.getenv('IMAP_USER')
    imap_pass = os.getenv('IMAP_PASS')
    webhook = os.getenv('FEISHU_WEBHOOKS')
    if not imap_user or not imap_pass or not webhook:
        raise ValueError("未提供 MAIL_ACCOUNTS_JSON，且缺少必要的单账户环境变量")

    cfg = DEFAULTS.copy()
    cfg['imap_server'] = os.getenv('IMAP_SERVER', cfg['imap_server'])
    cfg['imap_port'] = int(os.getenv('IMAP_PORT', cfg['imap_port']))
    cfg['mailbox'] = os.getenv('MAILBOX', cfg['mailbox'])
    cfg['block_before'] = int(os.getenv('BLOCK_BEFORE', cfg['block_before']))
    cfg['max_emails_per_run'] = int(os.getenv('MAX_EMAILS_PER_RUN', cfg['max_emails_per_run']))
    cfg['batch_delay'] = float(os.getenv('BATCH_DELAY', cfg['batch_delay']))
    cfg['poll_interval'] = int(os.getenv('POLL_INTERVAL', cfg['poll_interval']))
    cfg['blocked_keywords'] = os.getenv('BLOCKED_KEYWORDS', '')
    cfg['max_retries'] = int(os.getenv('MAX_RETRIES', cfg['max_retries']))
    cfg['reconnect_interval'] = int(os.getenv('RECONNECT_INTERVAL', cfg['reconnect_interval']))
    cfg['debug'] = os.getenv('DEBUG', 'false').lower() == 'true'
    cfg['imap_user'] = imap_user
    cfg['imap_pass'] = imap_pass
    cfg['feishu_webhook'] = webhook
    return [cfg]

# ---------- 安全解码函数（全局） ----------
def safe_decode(payload, charset):
    """安全解码字节内容，处理未知编码和错误"""
    if not payload:
        return ""
    try:
        return payload.decode(charset or 'utf-8', errors='replace')
    except (LookupError, UnicodeDecodeError):
        # 回退到常见编码
        for enc in ('utf-8', 'gb18030', 'latin-1'):
            try:
                return payload.decode(enc, errors='replace')
            except (LookupError, UnicodeDecodeError):
                continue
        # 最终使用 ascii
        return payload.decode('ascii', errors='replace')

def decode_mime_header(header_value):
    """解码 MIME 编码的邮件头，安全处理未知编码"""
    if not header_value:
        return ''
    decoded_parts = decode_header(header_value)
    result = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result.append(safe_decode(part, encoding))
        else:
            result.append(part)
    return ''.join(result)

def strip_html(html):
    """粗略去除 HTML 标签与实体,提取可读文本(用于正文预览)"""
    if not html:
        return ''
    # 去掉 script/style 内容
    html = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', html)
    # 块级标签转为换行
    html = re.sub(r'(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|table|ul|ol)>', '\n', html)
    # 去掉其余标签
    html = re.sub(r'<[^>]+>', ' ', html)
    # 常见 HTML 实体
    html = (html.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>')
                .replace('&quot;', '"').replace('&#39;', "'"))
    lines = [ln.strip() for ln in html.splitlines()]
    return '\n'.join(ln for ln in lines if ln).strip()

def get_email_body(msg):
    """提取邮件正文，优先纯文本，否则 HTML"""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = safe_decode(payload, part.get_content_charset())
                    break
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = strip_html(safe_decode(payload, part.get_content_charset()))
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = safe_decode(payload, msg.get_content_charset())
    return body.strip()

def get_attachments(msg):
    """收集邮件附件文件名(排除内联正文部分)"""
    names = []
    if not msg.is_multipart():
        return names
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        filename = part.get_filename()
        if filename:
            names.append(decode_mime_header(filename))
    return names

def parse_email_date(date_str):
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        return None

def contains_blocked_keyword(text, blocked_keywords):
    if not text or not blocked_keywords:
        return False
    text_lower = text.lower()
    return any(kw in text_lower for kw in blocked_keywords)

# ---------- IMAP 连接管理(连接复用) ----------
def _safe_logout(mail):
    """安全登出 IMAP 连接,忽略异常"""
    if mail:
        try:
            mail.logout()
        except Exception:
            pass

def imap_connect(config):
    """建立 IMAP SSL 连接并登录(发送 ID 命令模拟 Outlook 客户端)。

    连接建立后由调用方持有复用,不再每轮重建;
    登录失败/连接失败时抛异常,由调用方做退避重连。
    """
    mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
    if config['debug']:
        mail.debug = 4

    # 发送 ID 命令
    if 'ID' not in imaplib.Commands:
        imaplib.Commands['ID'] = ('AUTH', 'SELECTED', 'NONAUTH')
    try:
        mail._simple_command('ID', '("name" "Outlook" "version" "16.0" "os" "linux")')
        if config['debug']:
            print(f"[{config['imap_user']}] ID command sent")
    except Exception as e:
        if config['debug']:
            print(f"[{config['imap_user']}] ID command failed: {e}")

    mail.login(config['imap_user'], config['imap_pass'])
    if config['debug']:
        print(f"[{config['imap_user']}] Login successful")
    return mail

def imap_fetch_unseen(config, mail, limit=None):
    """在已建立的连接上拉取未读邮件。

    - 只在搜索阶段就限定最近 block_before 秒内的未读邮件(SEARCH ... SINCE),
      从源头过滤远古积压的未读邮件,避免无谓 fetch 旧邮件:
      既省流量,也避免旧邮件 fetch 失败产生报错。
    - 使用 BODY.PEEK[] 抓取:不会像 RFC822/BODY[] 那样隐式设置 \\Seen,
      保证"过滤跳过的邮件保持未读、推送成功才标记已读"的设计成立。
    - 返回 (raw_emails, failed_uids):
        raw_emails: [{'uid':.., 'raw_email':..}] 成功拉取的邮件
        failed_uids: [uid, ...] 单封 fetch 失败的 UID,由调用方跨轮计数,
                     达到上限后标记已读放弃,避免同一封邮件无限重试
    - 连续多封 fetch 失败视为连接异常,抛错由调用方触发重连。
    """
    typ, data = mail.select(config['mailbox'])
    if typ != 'OK':
        raise Exception(f"SELECT 失败: {data}")

    # 只搜索最近 block_before 秒内的未读邮件(SINCE 按邮件 Date 头过滤)
    criteria = ['UNSEEN']
    block_before = config.get('block_before')
    if block_before:
        since = (datetime.now(BJ_TZ) - timedelta(seconds=block_before)).strftime('%d-%b-%Y')
        criteria += ['SINCE', since]

    typ, data = mail.uid('search', None, *criteria)
    if typ != 'OK':
        raise Exception(f"SEARCH 失败: {data}")

    uid_list = data[0].split()
    if not uid_list:
        return [], []

    if limit and limit > 0:
        uid_list = uid_list[:limit]

    if config['debug']:
        print(f"[{config['imap_user']}] Found {len(uid_list)} unseen emails")

    raw_emails = []
    failed_uids = []
    consecutive_fetch_failures = 0  # 连续 fetch 失败计数,超过阈值视为连接异常
    for uid in uid_list:
        typ, msg_data = mail.uid('fetch', uid, '(BODY.PEEK[])')
        if typ != 'OK':
            if config['debug']:
                print(f"[{config['imap_user']}] 获取 UID {uid.decode()} 失败，跳过")
            consecutive_fetch_failures += 1
            failed_uids.append(uid.decode())
            # 连续多封失败更可能是连接已损坏而非单封问题,主动抛错让调用方重连
            if consecutive_fetch_failures >= 3:
                raise Exception(f"连续 {consecutive_fetch_failures} 个 UID fetch 失败,连接可能已异常,触发重连")
            continue
        consecutive_fetch_failures = 0  # 成功时重置计数,容忍单封偶发失败
        raw_email = None
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                raw_email = response_part[1]
                break
        if raw_email:
            raw_emails.append({'uid': uid.decode(), 'raw_email': raw_email})

    return raw_emails, failed_uids

def send_to_feishu(config, subject, from_addr, date_str, body_preview, attachments=None):
    webhook = config['feishu_webhook']
    if not webhook:
        return False

    attach_line = ''
    if attachments:
        attach_line = '**📎 附件：** ' + '、'.join(attachments) + '\n'

    card_content = (
        f"**👤 发件人：** {from_addr}\n"
        f"**📝 主题：** {subject}\n"
        f"**🕐 时间：** {date_str}\n"
        f"{attach_line}"
        f"**📄 正文预览：**\n{body_preview}"
    )
    card = {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {"title": {"tag": "plain_text", "content": "📧 新邮件通知"}, "template": "blue"},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": card_content
                }
            },
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "由邮件监控服务自动推送"}]}
        ]
    }
    payload = {"msg_type": "interactive", "card": card}

    try:
        resp = requests.post(webhook, json=payload, timeout=5)
        resp.raise_for_status()
        print(f"[{config['imap_user']}] ✅ 已推送邮件: {subject}")
        return True
    except Exception as e:
        print(f"[{config['imap_user']}] ❌ 卡片消息发送失败，尝试纯文本: {e}")
        text_content = (
            f"📧 **新邮件通知**\n\n"
            f"👤 发件人: {from_addr}\n"
            f"📝 主题: {subject}\n"
            f"🕐 时间: {date_str}\n"
            + (f"📎 附件: {'、'.join(attachments)}\n" if attachments else "")
            + f"📄 正文预览:\n{body_preview}"
        )
        fallback_payload = {"msg_type": "text", "content": {"text": text_content}}
        try:
            resp = requests.post(webhook, json=fallback_payload, timeout=5)
            resp.raise_for_status()
            print(f"[{config['imap_user']}] ✅ 已通过纯文本推送邮件: {subject}")
            return True
        except Exception as fallback_e:
            print(f"[{config['imap_user']}] ❌ 纯文本也发送失败: {fallback_e}")
            return False

def monitor_account(config):
    imap_user = config['imap_user']
    blocked_keywords = [kw.strip().lower() for kw in config['blocked_keywords'].split(',') if kw.strip()]
    max_retries = config['max_retries']
    reconnect_interval = config['reconnect_interval']
    failed_count = {}  # uid -> 连续推送失败次数(内存计数,进程重启后清零)

    print(f"🚀 启动监控账户: {imap_user}")
    print(f"   轮询间隔: {config['poll_interval']} 秒")
    print(f"   连接复用: 每 {reconnect_interval // 60} 分钟强制重连一次")
    print(f"   跳过 {config['block_before'] // 3600} 小时前的邮件")
    print(f"   每次最多处理: {config['max_emails_per_run']} 封")
    print(f"   推送失败重试上限: {max_retries} 次(超限后标记已读放弃)")
    print(f"   屏蔽词: {', '.join(blocked_keywords) if blocked_keywords else '无'}")
    print("-" * 50)

    mail = None
    conn_errors = 0            # 连续连接/登录失败次数,用于退避重连
    last_connect_time = 0.0

    while True:
        try:
            # ---- 无连接时建立(登录失败/连接失败由外层捕获并退避重试) ----
            if mail is None:
                mail = imap_connect(config)
                conn_errors = 0
                last_connect_time = time.time()

            # ---- 定时强制重连,避免长时间占用连接被服务器断开 ----
            if time.time() - last_connect_time > reconnect_interval:
                print(f"[{imap_user}] 连接使用超过 {reconnect_interval // 60} 分钟,重新连接...")
                _safe_logout(mail)
                mail = imap_connect(config)
                conn_errors = 0
                last_connect_time = time.time()

            if config['debug']:
                print(f"[{now_bj().strftime('%H:%M:%S')}] [{imap_user}] 检查邮件...")
            raw_emails, failed_uids = imap_fetch_unseen(config, mail, limit=config['max_emails_per_run'])

            # 单封 fetch 失败的 UID:跨轮计数,达到上限后标记已读放弃,避免同一封无限重试
            for uid in failed_uids:
                failed_count[uid] = failed_count.get(uid, 0) + 1
                if failed_count[uid] >= max_retries:
                    print(f"[{imap_user}] ⏭️  UID {uid} fetch 连续失败 {max_retries} 次,标记已读放弃(避免反复重试)")
                    try:
                        mail.uid('store', uid, '+FLAGS', '\\Seen')
                    except Exception:
                        pass
                    failed_count.pop(uid, None)
                else:
                    print(f"[{imap_user}] ⚠️  UID {uid} fetch 失败(第 {failed_count[uid]}/{max_retries} 次),保持未读待重试")

            if raw_emails:
                # 有新邮件才打印,无新邮件保持静默,避免每轮刷屏
                print(f"[{now_bj().strftime('%H:%M:%S')}] [{imap_user}] 📬 获取到 {len(raw_emails)} 封未读邮件")
                cutoff_time = datetime.now(timezone.utc) - timedelta(seconds=config['block_before'])

                for item in raw_emails:
                    uid = item['uid']
                    raw_email = item['raw_email']
                    try:
                        msg = email.message_from_bytes(raw_email)
                        subject = decode_mime_header(msg.get('Subject', ''))
                        from_addr = decode_mime_header(msg.get('From', 'Unknown'))
                        raw_date = msg.get('Date', 'Unknown')
                        body = get_email_body(msg)
                        attachments = get_attachments(msg)

                        mail_date = parse_email_date(raw_date)
                        if mail_date:
                            if mail_date.tzinfo is None:
                                mail_date = mail_date.replace(tzinfo=timezone.utc)
                            local_date = mail_date.astimezone(BJ_TZ)
                            formatted_date = local_date.strftime('%Y-%m-%d %H:%M')
                        else:
                            formatted_date = raw_date

                        # 时间过滤：不推送，不标记已读
                        if mail_date and mail_date < cutoff_time:
                            print(f"[{imap_user}] ⏭️  跳过旧邮件（保持未读）: {subject}")
                            continue

                        # 屏蔽词过滤：不推送，不标记已读
                        if contains_blocked_keyword(subject, blocked_keywords) or contains_blocked_keyword(body, blocked_keywords):
                            print(f"[{imap_user}] 🚫 命中屏蔽词（保持未读）: {subject}")
                            continue

                        # 推送重试已达上限:标记已读放弃,避免每轮反复重试
                        if failed_count.get(uid, 0) >= max_retries:
                            print(f"[{imap_user}] ⏭️  推送重试已达上限({max_retries} 次),标记已读放弃: {subject}")
                            mail.uid('store', uid, '+FLAGS', '\\Seen')
                            failed_count.pop(uid, None)
                            continue

                        # 尝试推送，成功后才标记已读
                        body_preview = body[:500] + ('...' if len(body) > 500 else '')
                        push_success = send_to_feishu(config, subject, from_addr, formatted_date, body_preview, attachments)

                        if push_success:
                            mail.uid('store', uid, '+FLAGS', '\\Seen')
                            failed_count.pop(uid, None)
                        else:
                            failed_count[uid] = failed_count.get(uid, 0) + 1
                            print(f"[{imap_user}] ⚠️  推送失败(第 {failed_count[uid]}/{max_retries} 次),邮件保持未读以便重试: {subject}")

                        # 每封邮件之间休眠,加少量随机抖动避免多账户同时打飞书
                        time.sleep(config['batch_delay'] + random.uniform(0, 0.5))

                    except Exception as e:
                        print(f"[{imap_user}] ⚠️  处理邮件时出错（保持未读）: {e}")

        except Exception as e:
            # 连接/会话异常:丢弃连接,带退避重连
            conn_errors += 1
            print(f"[{imap_user}] ⚠️  连接异常(累计 {conn_errors} 次): {e}")
            _safe_logout(mail)
            mail = None
        finally:
            pass

        # 连接正常:按轮询间隔休眠;连接异常:短退避后立即重连
        # 注意:不能用 continue 提前跳到这里,否则无邮件时会变成忙循环
        if mail is None:
            backoff = min(conn_errors, 6) * 10   # 10s 起步,60s 封顶
            print(f"[{imap_user}] ⏳ {backoff} 秒后重新连接...")
            time.sleep(backoff)
        else:
            time.sleep(config['poll_interval'])

def main():
    accounts = load_accounts()
    print(f"📡 共加载 {len(accounts)} 个邮箱账户")
    threads = []
    for acc in accounts:
        t = threading.Thread(target=monitor_account, args=(acc,), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

if __name__ == "__main__":
    main()
