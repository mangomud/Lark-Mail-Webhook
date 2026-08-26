import os
import time
import email
import imaplib
import ssl
import requests
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

# ---------- 环境变量 ----------
IMAP_SERVER = os.getenv('IMAP_SERVER', 'imap.163.com')
IMAP_PORT = int(os.getenv('IMAP_PORT', 993))
IMAP_USER = os.getenv('IMAP_USER')
IMAP_PASS = os.getenv('IMAP_PASS')              # 授权码，不是邮箱密码
MAILBOX = os.getenv('MAILBOX', 'INBOX')
WEBHOOK = os.getenv('FEISHU_WEBHOOKS')
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', 60))
BLOCK_BEFORE = int(os.getenv('BLOCK_BEFORE', 86400))   # 秒，默认24小时
MAX_EMAILS_PER_RUN = int(os.getenv('MAX_EMAILS_PER_RUN', 50))   # 每次最多处理邮件数
BATCH_DELAY = float(os.getenv('BATCH_DELAY', 2.0))              # 每封邮件之间延时（秒）
DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'

# ---------- 辅助函数 ----------
def decode_mime_header(header_value):
    """解码 MIME 编码的邮件头（如主题、发件人）"""
    if not header_value:
        return ''
    decoded_parts = decode_header(header_value)
    result = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(encoding if encoding else 'utf-8', errors='ignore'))
        else:
            result.append(part)
    return ''.join(result)

def get_email_body(msg):
    """提取邮件正文，优先纯文本，否则 HTML，并处理未知编码"""
    body = ""
    # 辅助函数：安全解码字节内容
    def safe_decode(payload, charset):
        if not payload:
            return ""
        # 尝试使用声明编码
        try:
            return payload.decode(charset or 'utf-8', errors='replace')
        except (LookupError, UnicodeDecodeError):
            # 回退到常见编码
            for enc in ['utf-8', 'gb18030', 'latin-1']:
                try:
                    return payload.decode(enc, errors='replace')
                except (LookupError, UnicodeDecodeError):
                    continue
            # 最终使用 ascii 并替换错误
            return payload.decode('ascii', errors='replace')

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset()
                    body = safe_decode(payload, charset)
                    break
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset()
                        body = safe_decode(payload, charset)
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset()
            body = safe_decode(payload, charset)
    return body.strip()

def parse_email_date(date_str):
    """解析邮件日期，返回 datetime 对象（可能带时区）"""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        return None

def imap_fetch_unseen(limit=None):
    """连接 IMAP 服务器，获取指定数量的未读邮件原始字节，并标记为已读"""
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    if DEBUG:
        mail.debug = 4

    # 注册并发送 ID 命令（模拟 Outlook，避免 Unsafe Login）
    if 'ID' not in imaplib.Commands:
        imaplib.Commands['ID'] = ('AUTH', 'SELECTED', 'NONAUTH')
    try:
        mail._simple_command('ID', '("name" "Outlook" "version" "16.0" "os" "linux")')
        if DEBUG:
            print("[DEBUG] ID command sent successfully")
    except Exception as e:
        if DEBUG:
            print(f"[WARN] ID command failed (ignored): {e}")

    mail.login(IMAP_USER, IMAP_PASS)
    if DEBUG:
        print("[DEBUG] Login successful")

    typ, data = mail.select(MAILBOX)
    if typ != 'OK':
        raise Exception(f"SELECT 失败: {data}")

    typ, data = mail.uid('search', None, 'UNSEEN')
    if typ != 'OK':
        raise Exception(f"SEARCH 失败: {data}")

    uid_list = data[0].split()
    if not uid_list:
        mail.logout()
        return []

    # 限制处理数量：只取前 N 个 UID（最新的通常在前？这里按服务器返回顺序）
    if limit and limit > 0:
        uid_list = uid_list[:limit]

    print(f"Found {len(uid_list)} unseen emails (limit={limit})")
    raw_emails = []
    for uid in uid_list:
        typ, msg_data = mail.uid('fetch', uid, '(RFC822)')
        if typ != 'OK':
            print(f"  ⚠️ 获取 UID {uid.decode()} 失败，跳过")
            continue
        raw_email = None
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                raw_email = response_part[1]
                break
        if raw_email:
            raw_emails.append(raw_email)
        # 标记为已读
        mail.uid('store', uid, '+FLAGS', '\\Seen')

    mail.logout()
    return raw_emails

def send_to_feishu(subject, from_addr, date_str, body_preview):
    """通过飞书自定义机器人发送通知，使用卡片消息，失败时回退纯文本"""
    if not WEBHOOK:
        return

    # 飞书卡片消息
    card = {
        "config": {
            "wide_screen_mode": True,
            "enable_forward": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "📧 新邮件通知"
            },
            "template": "blue"
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**👤 发件人：** {from_addr}\n"
                        f"**📝 主题：** {subject}\n"
                        f"**🕐 时间：** {date_str}\n\n"
                        f"**📄 正文预览：**\n"
                        f"```\n{body_preview}\n```"
                    )
                }
            },
            {
                "tag": "hr"
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "由邮件监控服务自动推送"
                    }
                ]
            }
        ]
    }

    payload = {
        "msg_type": "interactive",
        "card": card
    }

    try:
        resp = requests.post(WEBHOOK, json=payload, timeout=5)
        resp.raise_for_status()
        print(f"  ✅ 已推送邮件: {subject}")
    except Exception as e:
        print(f"  ❌ 卡片消息发送失败，尝试纯文本: {e}")
        # 回退纯文本
        text_content = (
            f"📧 **新邮件通知**\n\n"
            f"👤 发件人: {from_addr}\n"
            f"📝 主题: {subject}\n"
            f"🕐 时间: {date_str}\n\n"
            f"📄 正文预览:\n{body_preview}"
        )
        fallback_payload = {
            "msg_type": "text",
            "content": {"text": text_content}
        }
        try:
            resp = requests.post(WEBHOOK, json=fallback_payload, timeout=5)
            resp.raise_for_status()
            print(f"  ✅ 已通过纯文本推送邮件: {subject}")
        except Exception as fallback_e:
            print(f"  ❌ 纯文本也发送失败: {fallback_e}")

def main():
    print("🚀 启动邮件监控服务 (with ID command + rate limiting)")
    print(f"📧 邮箱: {IMAP_USER}")
    print(f"⏱️  轮询间隔: {POLL_INTERVAL} 秒")
    print(f"📅 跳过 {BLOCK_BEFORE // 3600} 小时前的邮件")
    print(f"🔢 每次最多处理: {MAX_EMAILS_PER_RUN} 封")
    print(f"⏳ 每封处理延时: {BATCH_DELAY} 秒")
    if DEBUG:
        print("🐞 调试模式已开启，所有 IMAP 交互将输出")
    print("-" * 50)

    while True:
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 检查邮件...")
            raw_emails = imap_fetch_unseen(limit=MAX_EMAILS_PER_RUN)
            if not raw_emails:
                print("📭 没有新邮件")
            else:
                print(f"📬 获取到 {len(raw_emails)} 封未读邮件")
                # 截止时间：当前 UTC 时间减去 BLOCK_BEFORE 秒
                cutoff_time = datetime.now(timezone.utc) - timedelta(seconds=BLOCK_BEFORE)

                for raw_email in raw_emails:
                    try:
                        msg = email.message_from_bytes(raw_email)
                        subject = decode_mime_header(msg.get('Subject', ''))
                        from_addr = decode_mime_header(msg.get('From', 'Unknown'))   # 解码发件人
                        raw_date = msg.get('Date', 'Unknown')
                        body = get_email_body(msg)

                        # 解析邮件日期
                        mail_date = parse_email_date(raw_date)
                        if mail_date:
                            if mail_date.tzinfo is None:
                                mail_date = mail_date.replace(tzinfo=timezone.utc)
                            local_date = mail_date.astimezone(timezone(timedelta(hours=8)))
                            formatted_date = local_date.strftime('%Y-%m-%d %H:%M')
                        else:
                            formatted_date = raw_date

                        # 时间过滤：跳过太旧的邮件
                        if mail_date and mail_date < cutoff_time:
                            print(f"  ⏭️  跳过旧邮件: {subject} ({mail_date})")
                            continue

                        # 发送通知
                        send_to_feishu(
                            subject=subject,
                            from_addr=from_addr,
                            date_str=formatted_date,
                            body_preview=body[:500] + ('...' if len(body) > 500 else '')
                        )

                        # 每封邮件之间延时，避免飞书限流
                        time.sleep(BATCH_DELAY)

                    except Exception as e:
                        print(f"  ⚠️  处理邮件时出错: {e}")
        except Exception as e:
            print(f"⚠️  主循环错误: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()