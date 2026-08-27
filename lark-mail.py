import os
import time
import json
import email
import imaplib
import threading
import requests
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

# ---------- 全局默认配置 ----------
DEFAULTS = {
    'imap_server': 'imap.163.com',
    'imap_port': 993,
    'mailbox': 'INBOX',
    'block_before': 86400,
    'max_emails_per_run': 50,
    'batch_delay': 2.0,
    'poll_interval': 60,
    'blocked_keywords': '',
    'debug': False,
}

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
                        body = safe_decode(payload, part.get_content_charset())
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = safe_decode(payload, msg.get_content_charset())
    return body.strip()

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

def imap_fetch_unseen_raw(config, limit=None):
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

    typ, data = mail.select(config['mailbox'])
    if typ != 'OK':
        raise Exception(f"SELECT 失败: {data}")

    typ, data = mail.uid('search', None, 'UNSEEN')
    if typ != 'OK':
        raise Exception(f"SEARCH 失败: {data}")

    uid_list = data[0].split()
    if not uid_list:
        mail.logout()
        return mail, []

    if limit and limit > 0:
        uid_list = uid_list[:limit]

    print(f"[{config['imap_user']}] Found {len(uid_list)} unseen emails")

    raw_emails = []
    for uid in uid_list:
        typ, msg_data = mail.uid('fetch', uid, '(RFC822)')
        if typ != 'OK':
            print(f"[{config['imap_user']}] 获取 UID {uid.decode()} 失败，跳过")
            continue
        raw_email = None
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                raw_email = response_part[1]
                break
        if raw_email:
            raw_emails.append({'uid': uid.decode(), 'raw_email': raw_email})

    return mail, raw_emails

def send_to_feishu(config, subject, from_addr, date_str, body_preview):
    webhook = config['feishu_webhook']
    if not webhook:
        return False

    card = {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {"title": {"tag": "plain_text", "content": "📧 新邮件通知"}, "template": "blue"},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**👤 发件人：** {from_addr}\n"
                        f"**📝 主题：** {subject}\n"
                        f"**🕐 时间：** {date_str}\n\n"
                        f"**📄 正文预览：**\n```\n{body_preview}\n```"
                    )
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
            f"🕐 时间: {date_str}\n\n"
            f"📄 正文预览:\n{body_preview}"
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

    print(f"🚀 启动监控账户: {imap_user}")
    print(f"   轮询间隔: {config['poll_interval']} 秒")
    print(f"   跳过 {config['block_before'] // 3600} 小时前的邮件")
    print(f"   每次最多处理: {config['max_emails_per_run']} 封")
    print(f"   屏蔽词: {', '.join(blocked_keywords) if blocked_keywords else '无'}")
    print("-" * 50)

    while True:
        mail = None
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [{imap_user}] 检查邮件...")
            mail, raw_emails = imap_fetch_unseen_raw(config, limit=config['max_emails_per_run'])
            if not raw_emails:
                print(f"[{imap_user}] 📭 没有新邮件")
                continue

            print(f"[{imap_user}] 📬 获取到 {len(raw_emails)} 封未读邮件")
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

                    mail_date = parse_email_date(raw_date)
                    if mail_date:
                        if mail_date.tzinfo is None:
                            mail_date = mail_date.replace(tzinfo=timezone.utc)
                        local_date = mail_date.astimezone(timezone(timedelta(hours=8)))
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

                    # 尝试推送，成功后才标记已读
                    body_preview = body[:500] + ('...' if len(body) > 500 else '')
                    push_success = send_to_feishu(config, subject, from_addr, formatted_date, body_preview)

                    if push_success:
                        mail.uid('store', uid, '+FLAGS', '\\Seen')
                    else:
                        print(f"[{imap_user}] ⚠️  推送失败，邮件保持未读以便重试: {subject}")

                    time.sleep(config['batch_delay'])

                except Exception as e:
                    print(f"[{imap_user}] ⚠️  处理邮件时出错（保持未读）: {e}")

        except Exception as e:
            print(f"[{imap_user}] ⚠️  主循环错误: {e}")
        finally:
            if mail:
                try:
                    mail.logout()
                except:
                    pass

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
