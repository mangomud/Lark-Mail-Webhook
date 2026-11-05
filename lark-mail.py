import os
import re
import time
import json
import email
import shutil
import imaplib
import random
import unicodedata
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
    'blocked_senders': '',           # 屏蔽发件人,逗号分隔,匹配 From 头(不区分大小写)
    'max_retries': 3,                # 推送失败的最大重试次数,超限后标记放弃
    'reconnect_interval': 600,       # 连接复用:每 600 秒(10 分钟)强制重连一次,降低被服务器风控概率
    'push_mode': 'watermark',        # 默认: 纯本地水位线,完全不碰邮件(不改已读/已回复/不加关键字)
    'mark_seen': False,              # 推送成功后【不】标已读
    'debug': False,
}

# ---------- 运行时数据文件路径(屏蔽词与水位线都放在同一目录) ----------
# 容器部署时该目录由 docker-compose 挂载到宿主机 ./data,宿主机上可直接编辑
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_UID_STATE_FILE = os.getenv('UID_STATE_FILE', '').strip() or os.path.join(_APP_DIR, 'uid_state.json')

def now_bj():
    """当前北京时间(日志时间戳使用,避免容器 UTC 时区差异)"""
    return datetime.now(BJ_TZ)

def load_accounts():
    """加载账户配置，优先解析 MAIL_ACCOUNTS_JSON，否则回退到单账户环境变量"""
    json_str = os.getenv('MAIL_ACCOUNTS_JSON', '').strip()
    # 容忍"看起来像空"的各种写法: 单账户模式下这行常常被随手填成这些值,
    # 直接丢给 json.loads 会报错,当成"未配置"处理更符合直觉。
    if json_str.lower() in ('', 'null', 'none', '{}', '[]', '""', "''"):
        json_str = ''
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
                    'push_mode': 'PUSH_MODE',
                    'mark_seen': 'MARK_SEEN',
                    'debug': 'DEBUG',
                }
                for key, env_var in env_map.items():
                    env_val = os.getenv(env_var)
                    if env_val is not None:
                        if key in ('debug', 'mark_seen'):
                            cfg[key] = env_val.strip().lower() == 'true'
                        elif key == 'push_mode':
                            cfg[key] = env_val.strip().lower()
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
                cfg['push_mode'] = str(cfg.get('push_mode') or 'watermark').strip().lower()
                cfg['mark_seen'] = (cfg['mark_seen'] if isinstance(cfg['mark_seen'], bool)
                                    else str(cfg['mark_seen']).strip().lower() == 'true')
                cfg['debug'] = bool(cfg['debug']) if isinstance(cfg['debug'], bool) else str(cfg['debug']).lower() == 'true'
                for r in ('imap_user', 'imap_pass', 'feishu_webhook'):
                    if r not in cfg or not cfg[r]:
                        raise ValueError(f"账户配置缺少字段: {r}")
                merged_accounts.append(cfg)
            return merged_accounts
        except Exception as e:
            # 把值的样子打出来(只打开头,不泄露完整凭据),否则用户对着
            # "Expecting value: line 1 column 1" 这种报错根本没法定位
            preview = json_str[:80].replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
            print("❌ 解析 MAIL_ACCOUNTS_JSON 失败")
            print(f"   错误: {e}")
            print(f"   值长度: {len(json_str)}")
            print(f"   值开头: {preview!r}")
            print("   常见原因:")
            print("     1. JSON 被换行拆成多行(必须写成【一整行】)")
            print("     2. 行首或行尾混入了 # 注释")
            print("     3. 用了单引号,JSON 只认双引号")
            print("     4. 只做单账户部署时,这一行应该留空")
            print("   排查: docker inspect lark-mail --format '{{range .Config.Env}}{{println .}}{{end}}' | grep MAIL_ACCOUNTS_JSON")
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
    cfg['blocked_senders'] = os.getenv('BLOCKED_SENDERS', '')
    cfg['max_retries'] = int(os.getenv('MAX_RETRIES', cfg['max_retries']))
    cfg['reconnect_interval'] = int(os.getenv('RECONNECT_INTERVAL', cfg['reconnect_interval']))
    cfg['push_mode'] = os.getenv('PUSH_MODE', cfg['push_mode']).strip().lower()
    cfg['mark_seen'] = os.getenv('MARK_SEEN', str(cfg['mark_seen'])).strip().lower() == 'true'
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

# 零宽字符/格式字符(BOM、零宽空格、零宽连接符等):不可见但会拆断子串匹配,
# 垃圾邮件常用其拆字规避屏蔽,匹配前一律剔除
_INVISIBLE_CHARS = {ord(c): None for c in '\u200b\u200c\u200d\u2060\ufeff'}

def normalize_for_match(text):
    """匹配前统一文本形态,保证屏蔽词与邮件内容在同一坐标系里比较:
    - NFKC 归一化: 全角字母/数字/符号/空格 -> 半角(如 'ＡＢＣ１２３' -> 'abc123')
    - casefold 折叠大小写: 比 lower() 更彻底(如德语 ß -> ss),中英文均适用
    - 剔除零宽/格式字符: '免\\u200b费' 与 '免费' 视为相同,防拆字规避
    - 剔除所有空白字符(含换行/NBSP/全角空格): 'no  reply' 与 'noreply'、
      '免 费' 与 '免费' 视为相同,防空格拆字规避
    屏蔽词与邮件文本两侧都走本函数,规则对称,不会出现单侧漏匹配。
    """
    if not text:
        return ''
    norm = unicodedata.normalize('NFKC', text).casefold()
    return ''.join(norm.translate(_INVISIBLE_CHARS).split())

def parse_blocked_items(raw_str):
    """解析屏蔽词/屏蔽发件人配置,返回 [(原始词, 归一化词), ...]:
    - 支持中英文逗号分隔
    - 去除条目首尾所有空白(含换行/NBSP/全角空格,兼容多行 env 场景)
    - 原始词仅用于日志显示,匹配一律用归一化词
    - 归一化后为空的条目丢弃,避免空串子串匹配命中所有邮件
    - 归一化后相同的条目去重(如 'OFFER'/'offer'/'ＯＦＦＥＲ' 只留一条),
      避免冗余匹配和启动日志条数虚高
    """
    items = []
    seen = set()
    for raw in re.split(r'[,，]', raw_str or ''):
        raw = raw.strip()
        if not raw:
            continue
        norm = normalize_for_match(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        items.append((raw, norm))
    return items

def match_blocked_keyword(text, blocked_items):
    """判断文本是否命中屏蔽词/屏蔽发件人。

    blocked_items: parse_blocked_items 的返回值 [(原始词, 归一化词), ...]
    文本与屏蔽词两侧都做 NFKC+casefold 归一化后再做子串匹配,
    任何一侧存在大小写/全角半角差异都不会漏匹配。
    返回命中的原始屏蔽词(用于日志定位是哪条规则),未命中返回 None。
    """
    if not text or not blocked_items:
        return None
    text_norm = normalize_for_match(text)
    if not text_norm:
        return None
    for raw, norm in blocked_items:
        if norm in text_norm:
            return raw
    return None

# ---------- 跨线程推送去重(所有账户线程共享) ----------
_pushed_ids = {}              # 邮件标识(Message-ID 或指纹) -> 推送时间戳
_pushed_lock = threading.Lock()

def _try_mark_pushed(mail_key):
    """尝试占位:返回 False 表示该邮件已推送过(重复),返回 True 表示占位成功可推送"""
    with _pushed_lock:
        if mail_key in _pushed_ids:
            return False
        _pushed_ids[mail_key] = time.time()
        return True

def _unmark_pushed(mail_key):
    """推送失败时移除占位,允许下轮重试"""
    with _pushed_lock:
        _pushed_ids.pop(mail_key, None)

def _cleanup_pushed():
    """清理 7 天前的旧记录,防止内存无限增长。

    返回本次清掉的条数。只在每轮轮询开始时调用一次(不是每封邮件都调),
    这样即使一天几千封,也只是一天扫 1440 次,而不是几万次。
    """
    removed = 0
    with _pushed_lock:
        cutoff = time.time() - 7 * 86400
        for k in [k for k, v in _pushed_ids.items() if v < cutoff]:
            _pushed_ids.pop(k, None)
            removed += 1
    return removed

# ---------- 屏蔽邮件本地记忆(命中屏蔽词/发件人的邮件保持未读,
# 但本地记一笔,下轮在拉取阶段直接跳过,避免每轮重复下载正文+重复打日志+挤占抓取额度) ----------
_blocked_mails = {}           # f"{imap_user}|{mailbox}|{uidvalidity}|{uid}" -> 记录时间戳
_blocked_lock = threading.Lock()
# 记录数硬上限:正常场景下时间窗口清理已足够(默认 25 小时窗口,个人邮箱几十条);
# 该上限仅防御极端配置(超大 BLOCK_BEFORE + 垃圾邮件洪峰)导致内存无界增长,
# 10 万条约 13MB,触顶后淘汰最旧的 10% 腾出空间
_BLOCKED_MEMO_MAX = 100000

def _remember_blocked_mail(mail_key):
    """记录一封已判定为屏蔽的邮件(按 UID 维度,进程内存活)"""
    with _blocked_lock:
        if len(_blocked_mails) >= _BLOCKED_MEMO_MAX:
            oldest = sorted(_blocked_mails.items(), key=lambda kv: kv[1])[:_BLOCKED_MEMO_MAX // 10]
            for k, _ in oldest:
                _blocked_mails.pop(k, None)
        _blocked_mails[mail_key] = time.time()

def _cleanup_blocked(block_before):
    """清理超过 block_before 窗口的记录:超窗后 SEARCH 的 SINCE 条件本就搜不到,记录可回收"""
    cutoff = time.time() - block_before - 3600
    with _blocked_lock:
        for k in [k for k, v in _blocked_mails.items() if v < cutoff]:
            _blocked_mails.pop(k, None)

# ---------- "已推送"状态记录方式 ----------
# 问题: \Seen 是【用户行为】,点开看一眼就置位。拿它当"处理过没有"的铁证,
#       等于让别人随手一划就篡改你的状态机 —— 邮件被读过就永久漏推。
#
# 默认方案: watermark —— 状态只存在本地 uid_state.json,【完全不碰邮件】。
#   不标已读、不标已回复、不加自定义关键字,连 STORE 命令都不发。
#   容器里跑着但对邮箱是纯只读的,任何人用这个邮箱都不会察觉到异常。
#   代价: 状态全在本地,data 卷必须挂载好;文件丢了就只能重扫(会跳过历史)。
#
# 下面两种模式会往邮件上写东西,只有明确需要时才用:
#   keyword   : 自定义 IMAP keyword($LarkMailPushed)。客户端一般不显示,
#               但确实改动了邮件,共用邮箱不建议。
#   answered  : 标准 \Answered 位。Outlook/Foxmail 会显示"已回复",共用邮箱禁用。
PUSH_MODE = os.getenv('PUSH_MODE', 'watermark').strip().lower()
CUSTOM_PUSH_FLAG = '$LarkMailPushed'      # 仅 PUSH_MODE=keyword 时使用
ANSWERED_FLAG = '\\Answered'              # 仅 PUSH_MODE=answered 时使用

# 首次部署时,时间窗内的历史邮件超过这个数量就只建基线、不补推,
# 免得第一次启动就把几十上百封旧邮件一次性推出去刷屏。
# 少于等于这个数量则直接推(反正就几封,漏了更可惜)。
FIRST_RUN_BASELINE_MIN = 5

# ---------- UID 水位线(加速路径) ----------
# UID 单调递增,记下已处理到的最大 UID 就能做增量,省掉每轮重复搜索。
# keyword 模式下它只是加速,丢了还能靠服务端标记恢复;
# watermark 模式下它就是唯一的正确性来源,所以必须持久化。
_uid_state = {}          # "user|mailbox" -> {"uidvalidity": "...", "last_uid": 123}
_uid_state_lock = threading.Lock()


def probe_state_file():
    """启动自检:验证状态文件所在目录可写。

    水位线只是加速路径,写不进去也不会漏信(服务端标记兜底),
    但写不进去意味着本地加速失效,还是值得提醒一句。
    """
    d = os.path.dirname(_UID_STATE_FILE) or '.'
    try:
        os.makedirs(d, exist_ok=True)
        probe = _UID_STATE_FILE + '.probe'
        with open(probe, 'w', encoding='utf-8') as f:
            f.write('ok')
        os.remove(probe)
        return True
    except Exception as e:
        print("=" * 60)
        print(f"⚠️  状态文件目录不可写: {d}  ({e})")
        print("   影响: UID 水位线无法持久化,每次重启都会退回全量搜索。")
        print("   但不会漏信 —— 服务端 \\Answered 标记才是权威状态。")
        print("   排查: docker-compose.yml 是否挂载了  ./data:/data")
        print("=" * 60)
        return False


def _read_state_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("内容不是 JSON 对象")
    return data


def load_uid_state():
    """读取 UID 水位线。主文件缺失/损坏时尝试 .bak,都失败才按冷启动处理。"""
    global _uid_state
    print(f"📂 状态文件: {_UID_STATE_FILE}")
    for path, label in ((_UID_STATE_FILE, ''), (_UID_STATE_FILE + '.bak', '(用备份恢复)')):
        if not os.path.isfile(path):
            continue
        try:
            data = _read_state_file(path)
        except Exception as e:
            print(f"⚠️  状态文件读取失败 {path}: {e}")
            continue
        _uid_state = data
        print(f"💾 已恢复 {len(_uid_state)} 个邮箱文件夹的水位线{label} —— 重启后继续增量拉取")
        return
    if not os.path.isfile(_UID_STATE_FILE):
        print("🆕 状态文件不存在: 首次运行,或 volume 没挂上")
    _uid_state = {}


def save_uid_state():
    """原子写入水位线文件(先写 .tmp 再 os.replace,避免进程被杀时留下半个 JSON)。

    watermark 模式下这个文件是【唯一】的状态来源,所以额外保留一份 .bak:
    主文件万一写坏/被误删,还能从备份恢复,不至于整个重扫。
    """
    with _uid_state_lock:
        snapshot = dict(_uid_state)
    first_time = not os.path.isfile(_UID_STATE_FILE)
    try:
        d = os.path.dirname(_UID_STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = _UID_STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        if os.path.isfile(_UID_STATE_FILE):
            try:
                shutil.copy2(_UID_STATE_FILE, _UID_STATE_FILE + '.bak')
            except Exception:
                pass
        os.replace(tmp, _UID_STATE_FILE)
        if first_time:
            # 首次落盘时明确告知一声,方便确认挂载目录真的可写、文件真的建出来了
            print(f"💾 已创建状态文件: {_UID_STATE_FILE}")
    except Exception as e:
        print(f"⚠️  水位线写入失败(不影响推送,但重启后会重复处理): {e}")


def get_watermark(state_key):
    """返回 (uidvalidity, last_uid);无记录时返回 (None, 0)"""
    with _uid_state_lock:
        st = _uid_state.get(state_key) or {}
    return st.get('uidvalidity'), int(st.get('last_uid') or 0)


def set_watermark(state_key, uidvalidity, last_uid):
    with _uid_state_lock:
        _uid_state[state_key] = {'uidvalidity': uidvalidity, 'last_uid': int(last_uid)}


def probe_custom_flag(mail):
    """探测服务器是否支持自定义 IMAP keyword。

    做法: 拿窗口内最后一封邮件,打一个探针标记 → SEARCH KEYWORD 验证 → 立刻撤销。
    无论结果如何都会撤销,绝不在用户的邮件上留痕。

    返回 True / False;探测不了(空邮箱 / 连接异常)返回 None。
    """
    try:
        typ, data = mail.uid('search', None, 'ALL')
        if typ != 'OK' or not data or not data[0]:
            return None                     # 空邮箱,没法探测
        uid = data[0].split()[-1].decode()
        probe = '$LarkProbe'
        typ, _ = mail.uid('store', uid, '+FLAGS', probe)
        if typ != 'OK':
            return False
        typ, d = mail.uid('search', None, 'KEYWORD', probe)
        supported = typ == 'OK' and d and d[0] and uid.encode() in d[0].split()
        try:
            mail.uid('store', uid, '-FLAGS', probe)   # 必须撤销探针
        except Exception:
            pass
        return bool(supported)
    except Exception as e:
        print(f"⚠️  自定义标记探测失败({e}),回退到本地水位线模式")
        return None


def resolve_push_mode(mail, config):
    """确定本账户实际的推送状态记录方式。

    默认 watermark —— 完全不碰邮件(不发任何 STORE)。
    keyword / answered 必须显式配置;keyword 还会先验证服务器是否真支持,
    不支持就自动退回 watermark,避免标记写不进去导致每轮重复推送。
    """
    mode = str(config.get('push_mode') or PUSH_MODE or 'watermark').strip().lower()
    if mode == 'watermark':
        return 'watermark'
    if mode == 'answered':
        return 'answered'          # 标准 flag,所有服务器都支持,无需探测
    if mode == 'keyword':
        try:
            mail.select(config['mailbox'])      # SEARCH 需要先 SELECT
        except Exception:
            pass
        if probe_custom_flag(mail):
            return 'keyword'
        print(f"[{config['imap_user']}] ⚠️  服务器不支持自定义 keyword,已退回 watermark(不碰邮件)")
        return 'watermark'
    print(f"[{config['imap_user']}] ⚠️  未知的 PUSH_MODE={mode},按 watermark 处理")
    return 'watermark'


PUSH_MODE_DESC = {
    'watermark': '本地 UID 水位线(完全不碰邮件: 不改已读、不改已回复、不加关键字)',
    'keyword': f'自定义 keyword {CUSTOM_PUSH_FLAG}(会改动邮件,共用邮箱不建议)',
    'answered': '\\Answered(客户端会显示"已回复"箭头,共用邮箱禁用)',
}


def _search_uids(mail, since, mode='unpushed', push_mode='keyword'):
    """按推送状态标记搜索 UID 集合。

    mode: 'unpushed'(未推送) / 'pushed'(已推送) / 'all'(全部)
    push_mode: keyword / answered / watermark
    搜索失败不抛错,返回空集。
    """
    if push_mode == 'keyword':
        criteria = {'unpushed': ['UNKEYWORD', CUSTOM_PUSH_FLAG],
                    'pushed': ['KEYWORD', CUSTOM_PUSH_FLAG],
                    'all': ['ALL']}[mode]
    elif push_mode == 'answered':
        criteria = {'unpushed': ['UNANSWERED'],
                    'pushed': ['ANSWERED'],
                    'all': ['ALL']}[mode]
    else:
        criteria = ['ALL']
    if since:
        criteria += ['SINCE', since]
    typ, data = mail.uid('search', None, *criteria)
    if typ != 'OK' or not data or not data[0]:
        return set()
    return {int(u) for u in data[0].split()}


def _mark_pushed_flag(mail, uid, push_mode):
    """把邮件标记为"已推送"。

    keyword 模式写自定义 keyword,answered 模式写 \\Answered,
    watermark 模式不碰邮件(只靠本地水位线)。
    """
    try:
        if push_mode == 'keyword':
            mail.uid('store', str(uid), '+FLAGS', CUSTOM_PUSH_FLAG)
        elif push_mode == 'answered':
            mail.uid('store', str(uid), '+FLAGS', ANSWERED_FLAG)
        return True
    except Exception:
        return False


def build_store_flags(push_mode, mark_seen):
    """构造推送成功后要写的 flag 列表"""
    flags = []
    if mark_seen:
        flags.append('\\Seen')
    if push_mode == 'keyword':
        flags.append(CUSTOM_PUSH_FLAG)
    elif push_mode == 'answered':
        flags.append(ANSWERED_FLAG)
    if not flags:
        return None                    # watermark + 不标已读 -> 什么都不用写
    return flags[0] if len(flags) == 1 else '(' + ' '.join(flags) + ')'


def _apply_push_state(mail, uid, push_mode, store_flags):
    """按当前模式把"已推送"状态写到邮件上(watermark 模式不碰邮件)"""
    try:
        if store_flags:
            mail.uid('store', str(uid), '+FLAGS', store_flags)
        elif push_mode != 'watermark':
            _mark_pushed_flag(mail, uid, push_mode)
    except Exception:
        pass


# ---------- IMAP 连接管理(连接复用) ----------
def _safe_logout(mail):
    """安全登出 IMAP 连接,忽略异常"""
    if mail:
        try:
            mail.logout()
        except Exception:
            pass

# 常见邮箱服务商的默认 IMAP 地址,用于登录失败时做配置矛盾检查
KNOWN_IMAP = {
    '163.com': 'imap.163.com',
    '126.com': 'imap.126.com',
    'yeah.net': 'imap.yeah.net',
    'qq.com': 'imap.qq.com',
    'foxmail.com': 'imap.qq.com',
    'gmail.com': 'imap.gmail.com',
    'outlook.com': 'outlook.office365.com',
    'hotmail.com': 'outlook.office365.com',
    'live.com': 'outlook.office365.com',
}


def hint_imap_config(config):
    """登录失败时把最常见的一类配置错误指出来。

    最高频的坑: 用的是企业邮箱(如 @company.com),但 IMAP_SERVER 还留着默认的
    imap.163.com。服务器当然回"用户名或密码错误" —— 它压根不认识这个账号。
    光看这条错误,人很容易去怀疑授权码,来回折腾半天。
    """
    user = config.get('imap_user', '') or ''
    server = (config.get('imap_server', '') or '').lower()
    if '@' not in user:
        return
    domain = user.split('@')[-1].lower()

    expected = KNOWN_IMAP.get(domain)
    if expected:
        ok = expected in server
        advice = f"应该是 {expected}:993"
    else:
        # 企业邮箱(域名不在已知表里): 只要不是那个默认的 163 地址,就当用户已经改对了
        ok = server != 'imap.163.com'
        advice = ("企业邮箱需要填服务商自己的 IMAP 地址,常见的有:\n"
                  "        腾讯企业邮 imap.exmail.qq.com:993\n"
                  "        阿里企业邮 imap.qiye.aliyun.com:993\n"
                  "        网易企业邮 imap.qiye.163.com:993  (注意不是 imap.163.com)\n"
                  f"        查一下: dig +short MX {domain}")

    if ok:
        # 地址看着是对的,那问题多半在授权码
        if '163' in server or '126' in server:
            print(f"[{user}] 💡 如果反复这个错, 先确认 IMAP_PASS 填的是【授权码】而不是登录密码")
        return

    print(f"[{user}] 💡 配置对不上: 邮箱域名是 {domain}, 但 IMAP_SERVER 指向 {server}")
    print(f"        {advice}")
    print(f"        改完执行: docker compose up -d --force-recreate  (restart 不会重读 .env)")


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

def imap_fetch_new(config, mail, limit=None, retry_uids=None, push_mode='keyword'):
    """拉取"还没推送过"的邮件,与已读/未读无关。

    push_mode 决定用哪种状态记录方式:
      keyword   : 服务端自定义 keyword,冷启动可靠恢复,不干扰共享邮箱
      answered  : 服务端 \\Answered 位(兼容性最好,但共享邮箱有副作用)
      watermark : 纯本地 UID 水位线,完全不碰邮件

    搜索策略:
      1. 常规增量: UID <水位线+1>:* —— 水位线之后的所有邮件,不区分已读未读。
      2. 冷启动(首次部署 / 本地文件丢失 / UIDVALIDITY 变化):
         keyword/answered 模式退回服务端标记全量搜索,已读的也能准确捞回来;
         watermark 模式没标记可用,只能跳过历史(端上漏信风险最高的一种)。
      3. 兜底(仅当 retry_uids 非空时): 再搜一次未推送的,捞回推送失败的。

    - BODY.PEEK[] 抓取:不会隐式设置 \\Seen。
    - 本地已记录的屏蔽邮件直接跳过,且在套 limit 之前剔除,避免挤占每轮抓取额度。
    - 水位线只推进到"本轮真正 fetch 成功"的最大 UID:被 limit 截断或 fetch 失败的邮件
      UID 仍在水位线之上,下轮还能被增量搜索捞到,不会因为额度限制漏水。

    返回 (raw_emails, failed_uids, uidvalidity, state_key)
    """
    typ, data = mail.select(config['mailbox'])
    if typ != 'OK':
        raise Exception(f"SELECT 失败: {data}")

    # UIDVALIDITY 只能取一次(imaplib 取后即清除)
    resp = mail.response('UIDVALIDITY')
    uidvalidity = resp[1][0].decode() if resp and resp[1] else '0'
    state_key = f"{config['imap_user']}|{config['mailbox']}"
    blocked_prefix = f"{state_key}|{uidvalidity}"

    saved_vid, saved_uid = get_watermark(state_key)
    cold_start = (saved_vid is None) or (saved_vid != uidvalidity)
    watermark = 0 if cold_start else saved_uid

    def _commit(final_uid):
        """水位线只在真正推进或有变化时落盘,减少无谓 IO"""
        if final_uid > saved_uid or saved_vid != uidvalidity:
            set_watermark(state_key, uidvalidity, final_uid)
            save_uid_state()

    # 时间窗(SINCE 按邮件 Date 头过滤),从源头挡住远古积压
    block_before = config.get('block_before')
    since = (datetime.now(BJ_TZ) - timedelta(seconds=block_before)).strftime('%d-%b-%Y') if block_before else None

    candidate_uids = set()
    baseline_ceiling = 0      # 冷启动时窗口内的最大 UID,用于"没有待推邮件"时直接推进水位线

    if cold_start and push_mode == 'watermark':
        # 纯本地模式: 服务端没有任何可用标记,只能靠"跨过历史"防刷屏。
        # 代价必须说清楚 —— 本地水位线丢过时,期间到达且被人读过的邮件会漏。
        # 所以默认优先 keyword,watermark 只是服务器不支持时的兜底。
        all_uids = _search_uids(mail, since, 'all', push_mode)
        watermark = max(all_uids) if all_uids else 0
        baseline_ceiling = watermark
        if all_uids:
            print(f"[{config['imap_user']}] 🩹 冷启动(纯本地模式): 水位线起点设为 UID {watermark},"
                  f"跳过 {len(all_uids)} 封历史邮件")
            print(f"[{config['imap_user']}]    ⚠️  此模式依赖本地状态文件,若 data 卷丢失会漏掉期间已读的邮件")
        _commit(watermark)
        return [], [], uidvalidity, state_key

    if cold_start:
        # 本地水位线不可信。绝不能靠"直接把整个时间窗跨过去"来防刷屏 ——
        # 那样会把停机期间到达、并且已经被人读过的邮件一并跨掉,正是要治的漏信。
        # 改为问服务器: 窗口内哪些邮件还没打过推送标记?
        all_uids = _search_uids(mail, since, 'all', push_mode)
        unpushed_uids = _search_uids(mail, since, 'unpushed', push_mode)

        if not all_uids:
            if config['debug']:
                print(f"[{config['imap_user']}] 冷启动: 时间窗内没有邮件")
        elif (saved_vid is None and len(unpushed_uids) == len(all_uids)
              and len(all_uids) > FIRST_RUN_BASELINE_MIN):
            # 首次部署: 窗口内一封带标记的都没有,说明服务端也没有我们的历史记录。
            # 这种情况全推会把一堆历史邮件一次性轰出去,先建立基线:
            # 全部打上标记但不推送,之后新到的邮件正常推。
            print(f"[{config['imap_user']}] 🆕 首次部署: 为时间窗内 {len(all_uids)} 封历史邮件建立基线(不推送)")
            marked = sum(1 for u in sorted(all_uids) if _mark_pushed_flag(mail, u, push_mode))
            watermark = max(all_uids)
            _commit(watermark)
            print(f"[{config['imap_user']}] ✅ 基线建立完成({marked}/{len(all_uids)} 封已标记),新到的邮件会正常推送")
            return [], [], uidvalidity, state_key
        else:
            # 服务端有历史标记 → 说明只是本地状态丢了(重启/换机器/文件被删),
            # 或者 UIDVALIDITY 变了。按"未推送"补推:
            # **已经被人读过、但没推送过的邮件也会在这里被捞回来**,这就是要的效果。
            candidate_uids = unpushed_uids
            watermark = (min(all_uids) - 1) if all_uids else 0
            baseline_ceiling = max(all_uids) if all_uids else 0
            print(f"[{config['imap_user']}] 🩹 冷启动恢复: 服务端标记显示 {len(unpushed_uids)} 封未推送,"
                  f"已列入候选(含被读过的)")
    else:
        # 常规增量:水位线之后的所有邮件,不区分已读
        typ, data = mail.uid('search', None, 'UID', f'{watermark + 1}:*')
        if typ != 'OK':
            raise Exception(f"SEARCH 失败: {data}")
        for u in (data[0].split() if data and data[0] else []):
            uid_i = int(u)
            # 坑: 'n:*' 在 n 大于邮箱当前最大 UID 时会退化成返回最大那封,必须再按水位线滤一次
            if uid_i > watermark:
                candidate_uids.add(uid_i)
        if config['debug']:
            print(f"[{config['imap_user']}] 增量搜索: 水位线 UID={watermark}, 新邮件 {len(candidate_uids)} 封")

    # 兜底:仅在存在待重试的 UID 时才额外搜一次"未推送",捞回推送失败的邮件。
    # 这里绝不能用 UNSEEN —— 推送失败期间邮件可能已经被同事点开读过,
    # 用 UNSEEN 兜底会直接把这封漏掉。
    # 正常无失败时跳过,否则每轮都要把历史未推送邮件重新 fetch 一遍,白烧流量。
    if retry_uids and not cold_start and push_mode != 'watermark':
        before = len(candidate_uids)
        candidate_uids |= _search_uids(mail, since, 'unpushed', push_mode)
        if len(candidate_uids) > before:
            print(f"[{config['imap_user']}] 🔁 兜底捞回 {len(candidate_uids) - before} 封未推送邮件")

    if not candidate_uids:
        # 冷启动时窗口内可能一封待推的都没有(全被标记过)。
        # 这时候必须把水位线推到窗口最大 UID,否则它会停在 0,
        # 下一轮又变成"增量搜索 UID 1:*"把整个邮箱重新扫一遍。
        _commit(max(watermark, baseline_ceiling))
        return [], [], uidvalidity, state_key

    # 先剔除本地已记录的屏蔽邮件,再套 limit:屏蔽邮件不占抓取名额
    skipped_blocked = 0
    pending = []
    for uid_i in sorted(candidate_uids):
        if f"{blocked_prefix}|{uid_i}" in _blocked_mails:
            skipped_blocked += 1
            continue
        pending.append(uid_i)
    if skipped_blocked:
        print(f"[{config['imap_user']}] ⏭️  跳过 {skipped_blocked} 封已知屏蔽邮件(保持未读,不占抓取额度)")

    if not pending:
        _commit(watermark)
        return [], [], uidvalidity, state_key

    if limit and limit > 0:
        pending = pending[:limit]
    print(f"[{config['imap_user']}] 📩 实际拉取 {len(pending)} 封邮件进行过滤判断")

    raw_emails = []
    failed_uids = []
    consecutive_fetch_failures = 0  # 连续 fetch 失败计数,超过阈值视为连接异常
    for uid_i in pending:
        typ, msg_data = mail.uid('fetch', str(uid_i), '(BODY.PEEK[])')
        if typ != 'OK':
            if config['debug']:
                print(f"[{config['imap_user']}] 获取 UID {uid_i} 失败，跳过")
            consecutive_fetch_failures += 1
            failed_uids.append(str(uid_i))
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
            raw_emails.append({'uid': str(uid_i), 'raw_email': raw_email})

    # 水位线推进到本轮成功 fetch 的最大 UID;被 limit 截断的、fetch 失败的都还在水位线之上,下轮可重来
    final_uid = watermark
    if raw_emails:
        final_uid = max(final_uid, max(int(r['uid']) for r in raw_emails))
    _commit(final_uid)

    return raw_emails, failed_uids, uidvalidity, state_key

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
        f"**📄 正文预览：**\n```\n{body_preview}\n```"
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
    except requests.exceptions.ReadTimeout as e:
        # 读取超时:请求已发出,飞书大概率已收到卡片,
        # 此时回退纯文本会造成同一条消息双发,故按"已发送"处理
        print(f"[{config['imap_user']}] ⚠️  卡片消息响应超时(为避免重复不再回退纯文本,视作已发送): {e}")
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
    # 屏蔽词/屏蔽发件人: 支持中英文逗号分隔;
    # 匹配前两侧统一做 NFKC 归一化(全角->半角)+casefold 折叠,
    # 大小写/全角半角差异都不会漏匹配; 原始词保留用于日志显示
    blocked_keywords = parse_blocked_items(config['blocked_keywords'])
    blocked_senders = parse_blocked_items(config['blocked_senders'])
    kw_display = ', '.join(kw for kw, _ in blocked_keywords)
    sender_display = ', '.join(s for s, _ in blocked_senders)
    max_retries = config['max_retries']
    reconnect_interval = config['reconnect_interval']
    mark_seen = config.get('mark_seen', True)
    failed_count = {}  # uid -> 连续推送失败次数(内存计数,进程重启后清零)
    push_mode = None   # 连上服务器后探测确定

    print(f"🚀 启动监控账户: {imap_user}")
    print(f"   轮询间隔: {config['poll_interval']} 秒")
    print(f"   连接复用: 每 {reconnect_interval // 60} 分钟强制重连一次")
    print(f"   跳过 {config['block_before'] // 3600} 小时前的邮件")
    print(f"   每次最多处理: {config['max_emails_per_run']} 封")
    print(f"   推送失败重试上限: {max_retries} 次(超限后标记放弃)")
    print(f"   状态记录方式: {config.get('push_mode', 'watermark')}(watermark = 完全不碰邮件)")
    print(f"   推送成功后标记已读: {'是' if mark_seen else '否(邮件保持原样)'}")
    print(f"   屏蔽词({len(blocked_keywords)} 条): {kw_display if kw_display else '无'}(忽略大小写/全半角/空白/零宽字符差异,匹配主题和正文)")
    print(f"   屏蔽发件人({len(blocked_senders)} 条): {sender_display if sender_display else '无'}(同上规则,匹配 From 头)")
    if blocked_keywords:
        print(f"   屏蔽词命中后: 不推送、保持未读、本地记录后不再重复处理")
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
                # 连接建立后确定状态记录方式(只需一次)
                if push_mode is None:
                    push_mode = resolve_push_mode(mail, config)
                    print(f"[{imap_user}] 📌 状态记录方式: {PUSH_MODE_DESC.get(push_mode, push_mode)}")

            # ---- 定时强制重连,避免长时间占用连接被服务器断开 ----
            if time.time() - last_connect_time > reconnect_interval:
                print(f"[{imap_user}] 连接使用超过 {reconnect_interval // 60} 分钟,重新连接...")
                _safe_logout(mail)
                mail = imap_connect(config)
                conn_errors = 0
                last_connect_time = time.time()

            if config['debug']:
                print(f"[{now_bj().strftime('%H:%M:%S')}] [{imap_user}] 检查邮件...")

            _cleanup_blocked(config['block_before'])
            # 去重记录每轮清一次即可(7 天窗口)。原来放在"每封推送成功"里,
            # 一天几千封就要重复遍历几千遍,纯属浪费 CPU。
            _cleanup_pushed()
            # 推送成功后要写的 flag:已读(可选)+ 推送标记(按模式)
            store_flags = build_store_flags(push_mode, mark_seen)
            raw_emails, failed_uids, uidvalidity, state_key = imap_fetch_new(
                config, mail, limit=config['max_emails_per_run'],
                retry_uids=failed_count, push_mode=push_mode)
            blocked_prefix = f"{state_key}|{uidvalidity}"

            # 单封 fetch 失败的 UID:跨轮计数,达到上限后标记放弃,避免同一封无限重试
            # (同时打推送标记:表示"这封不再处理",否则下一轮搜索又会把它捞回来)
            for uid in failed_uids:
                failed_count[uid] = failed_count.get(uid, 0) + 1
                if failed_count[uid] >= max_retries:
                    print(f"[{imap_user}] ⏭️  UID {uid} fetch 连续失败 {max_retries} 次,标记放弃(避免反复重试)")
                    try:
                        if store_flags:
                            mail.uid('store', uid, '+FLAGS', store_flags)
                        elif push_mode != 'watermark':
                            _mark_pushed_flag(mail, uid, push_mode)
                    except Exception:
                        pass
                    failed_count.pop(uid, None)
                else:
                    print(f"[{imap_user}] ⚠️  UID {uid} fetch 失败(第 {failed_count[uid]}/{max_retries} 次),保持未读待重试")

            if raw_emails:
                # 有新邮件才打印,无新邮件保持静默,避免每轮刷屏
                print(f"[{now_bj().strftime('%H:%M:%S')}] [{imap_user}] 📬 获取到 {len(raw_emails)} 封新邮件")
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

                        # 时间过滤：不推送，不写任何标记
                        if mail_date and mail_date < cutoff_time:
                            print(f"[{imap_user}] ⏭️  跳过旧邮件（保持未读）: {subject}")
                            continue

                        # 屏蔽词过滤:主题/正文分别匹配,日志记录命中的规则与位置,便于排查
                        hit_subject = match_blocked_keyword(subject, blocked_keywords)
                        hit_body = None if hit_subject else match_blocked_keyword(body, blocked_keywords)
                        if hit_subject or hit_body:
                            hit_kw = hit_subject or hit_body
                            hit_where = '主题' if hit_subject else '正文'
                            print(f"[{imap_user}] 🚫 命中屏蔽词[{hit_where}] 规则: {hit_kw} | 保持未读,后续轮次不再处理 | {subject}")
                            _remember_blocked_mail(f"{blocked_prefix}|{uid}")
                            continue

                        # 发件人屏蔽过滤:日志记录命中的规则
                        hit_sender = match_blocked_keyword(from_addr, blocked_senders)
                        if hit_sender:
                            print(f"[{imap_user}] 🚫 命中发件人屏蔽 规则: {hit_sender} | 保持未读,后续轮次不再处理 | {from_addr} / {subject}")
                            _remember_blocked_mail(f"{blocked_prefix}|{uid}")
                            continue

                        # 邮件唯一标识:优先 Message-ID,缺失时用 发件人+主题+原始Date头 指纹
                        message_id = (msg.get('Message-ID') or '').strip()
                        mail_key = message_id or f'{from_addr}|{subject}|{raw_date}'

                        # 去重占位:已推送过的邮件跳过重复推送,并顺手补标记
                        # (防止"推送成功但标记失败"或同邮箱多账户导致的重推)
                        if not _try_mark_pushed(mail_key):
                            print(f"[{imap_user}] ⏭️  该邮件已推送过,跳过重复推送: {subject}")
                            _apply_push_state(mail, uid, push_mode, store_flags)
                            continue

                        # 推送重试已达上限:标记放弃,避免每轮反复重试
                        if failed_count.get(uid, 0) >= max_retries:
                            print(f"[{imap_user}] ⏭️  推送重试已达上限({max_retries} 次),标记放弃: {subject}")
                            _apply_push_state(mail, uid, push_mode, store_flags)
                            failed_count.pop(uid, None)
                            continue

                        # 尝试推送
                        body_preview = body[:500] + ('...' if len(body) > 500 else '')
                        push_success = send_to_feishu(config, subject, from_addr, formatted_date, body_preview, attachments)

                        if push_success:
                            # 推送成功后写状态。按模式不同:
                            #   watermark-> 默认,不碰邮件,只靠本地水位线
                            #   keyword  -> 自定义 keyword(存服务器)
                            #   answered -> \Answered(客户端会显示"已回复")
                            # \Seen 是否一起写由 MARK_SEEN 控制,共用邮箱可以关掉。
                            try:
                                if store_flags:
                                    mail.uid('store', uid, '+FLAGS', store_flags)
                                elif push_mode != 'watermark':
                                    _mark_pushed_flag(mail, uid, push_mode)
                            except Exception as e:
                                # 标记失败不影响本次推送,下轮会被内存去重拦住并补标记
                                print(f"[{imap_user}] ⚠️  标记状态失败(下轮会补标记): {e}")
                            failed_count.pop(uid, None)
                        else:
                            # 推送失败:移除去重占位,允许下轮重试
                            _unmark_pushed(mail_key)
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
            if conn_errors == 1:
                # 只在第一次失败时给配置排查提示,避免每 10 秒刷一遍
                try:
                    hint_imap_config(config)
                except Exception:
                    pass
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
    probe_state_file()
    load_uid_state()
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
