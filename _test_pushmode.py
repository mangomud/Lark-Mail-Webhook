# -*- coding: utf-8 -*-
"""状态记录方式自测。

核心要求: 默认(watermark)模式下,脚本对邮件【完全只读】——
不改已读、不改已回复、不加任何关键字,一条 STORE 命令都不发。
"""
import os
import sys
import types
import tempfile

_fake_requests = types.ModuleType('requests')
def _no_push(*a, **k):
    raise RuntimeError('测试环境不推送')
_fake_requests.post = _no_push
class _FakeExc:
    class ReadTimeout(Exception):
        pass
_fake_requests.exceptions = _FakeExc
sys.modules['requests'] = _fake_requests

_TMP = tempfile.mkdtemp()
os.environ['UID_STATE_FILE'] = os.path.join(_TMP, 'uid_state.json')

import importlib.util
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lark-mail.py')
spec = importlib.util.spec_from_file_location("larkmail", _SRC)
lm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lm)

CFG = {
    'imap_user': 'shared@163.com',
    'mailbox': 'INBOX',
    'block_before': 86400,
    'mark_seen': False,
    'debug': False,
}


class FakeMail:
    """模拟 IMAP。额外记录每一次 store,用来断言"有没有动过邮件"。"""

    def __init__(self, uidvalidity=b'777', supports_keywords=True):
        self.uidvalidity = uidvalidity
        self.supports_keywords = supports_keywords
        self.mails = {}
        self.store_calls = []     # [(uid, flags)]

    def add(self, uid, seen=False, answered=False, kw=None):
        self.mails[uid] = {'seen': seen, 'answered': answered, 'kw': set(kw or ())}

    def select(self, mailbox):
        return ('OK', [b'1'])

    def response(self, name):
        return ('UIDVALIDITY', [self.uidvalidity])

    def uid(self, cmd, *args):
        cmd = cmd.lower()
        if cmd == 'search':
            conds = [str(a) for a in args if a is not None]
            uids = sorted(self.mails)
            for c in conds:
                if ':*' in c:
                    n = int(c.split(':')[0])
                    mx = max(uids) if uids else 0
                    if n > mx:
                        uids = [mx] if mx else []
                    else:
                        uids = [u for u in uids if u >= n]
            i = 0
            while i < len(conds):
                c = conds[i]
                if c in ('KEYWORD', 'UNKEYWORD') and i + 1 < len(conds):
                    flag = conds[i + 1]
                    if c == 'KEYWORD':
                        uids = [u for u in uids if flag in self.mails[u]['kw']]
                    else:
                        uids = [u for u in uids if flag not in self.mails[u]['kw']]
                    i += 2
                    continue
                if c == 'ANSWERED':
                    uids = [u for u in uids if self.mails[u]['answered']]
                elif c == 'UNANSWERED':
                    uids = [u for u in uids if not self.mails[u]['answered']]
                elif c == 'UNSEEN':
                    uids = [u for u in uids if not self.mails[u]['seen']]
                i += 1
            return ('OK', [b' '.join(str(u).encode() for u in uids)])

        if cmd == 'fetch':
            uid = int(args[0])
            if uid not in self.mails:
                return ('NO', [None])
            return ('OK', [(b'1 (BODY[])',
                            b'Message-ID: <m%d@test>\r\nSubject: mail %d\r\n\r\nbody' % (uid, uid))])

        if cmd == 'store':
            uid = int(args[0])
            op = str(args[1]).upper() if len(args) > 1 else '+FLAGS'
            raw = ' '.join(str(a) for a in args[2:])
            flags = raw.strip('()').split()
            self.store_calls.append((uid, raw))
            if uid not in self.mails:
                return ('OK', [b'ok'])
            custom = [f for f in flags if f.startswith('$')]
            if custom and not self.supports_keywords:
                return ('NO', [b'[CANNOT] custom keywords not supported'])
            for f in flags:
                if f == '\\Seen':
                    self.mails[uid]['seen'] = True
                elif f == '\\Answered':
                    self.mails[uid]['answered'] = True
                elif f.startswith('$'):
                    if op.startswith('+'):
                        self.mails[uid]['kw'].add(f)
                    else:
                        self.mails[uid]['kw'].discard(f)
            return ('OK', [b'ok'])

        return ('OK', [b''])


def wipe_local_state():
    lm._uid_state = {}
    for p in (lm._UID_STATE_FILE, lm._UID_STATE_FILE + '.bak'):
        if os.path.exists(p):
            os.remove(p)


def run(mail, mode='watermark', limit=50, retry=None):
    lm.load_uid_state()
    raw, failed, vid, key = lm.imap_fetch_new(
        CFG, mail, limit=limit, retry_uids=retry, push_mode=mode)
    return [r['uid'] for r in raw]


print("=" * 68)
print("场景 1: 默认配置 = watermark + 不标已读")
print("=" * 68)
assert lm.DEFAULTS['push_mode'] == 'watermark', lm.DEFAULTS['push_mode']
assert lm.DEFAULTS['mark_seen'] is False
print(f"  ✅ push_mode={lm.DEFAULTS['push_mode']}, mark_seen={lm.DEFAULTS['mark_seen']}\n")

print("=" * 68)
print("场景 2 【核心】: 默认模式解析不探测、不发任何 STORE")
print("=" * 68)
m = FakeMail(supports_keywords=True)
m.add(1, seen=False)
mode = lm.resolve_push_mode(m, CFG)
assert mode == 'watermark', mode
assert m.store_calls == [], f"❌ 解析阶段就动了邮件: {m.store_calls}"
print(f"  ✅ 解析结果 {mode},store 调用 0 次\n")

print("=" * 68)
print("场景 3 【核心】: watermark 端到端 —— 推送完邮件状态一字节没变")
print("=" * 68)
wipe_local_state()
m3 = FakeMail(uidvalidity=b'800')
for u in (1, 2, 3):
    m3.add(u, seen=False, answered=False)
run(m3, mode='watermark')          # 冷启动建基线,历史邮件跳过
m3.add(4, seen=False)              # 基线之后新到一封
got = run(m3, mode='watermark')
assert got == ['4'], got
flags = lm.build_store_flags('watermark', False)
assert flags is None, flags
for u in got:
    lm._apply_push_state(m3, u, 'watermark', flags)
assert m3.store_calls == [], f"❌ watermark 模式下不该有任何 STORE: {m3.store_calls}"
assert all(not v['seen'] and not v['answered'] and not v['kw']
           for v in m3.mails.values()), "❌ 邮件状态被改动了"
print("  ✅ 新邮件推送成功,邮件状态完全没动,STORE 调用 0 次\n")

print("=" * 68)
print("场景 4: watermark 模式下 .bak 备份与恢复")
print("=" * 68)
assert os.path.isfile(lm._UID_STATE_FILE), "主状态文件没生成"
m3.add(4, seen=True)
run(m3, mode='watermark')
assert os.path.isfile(lm._UID_STATE_FILE + '.bak'), "备份没生成"
with open(lm._UID_STATE_FILE, 'w', encoding='utf-8') as f:
    f.write('{ broken json')
lm._uid_state = {}
lm.load_uid_state()
assert lm._uid_state, "主文件坏了之后没能从 .bak 恢复"
print(f"  ✅ 主文件写坏后从备份恢复,{len(lm._uid_state)} 个文件夹\n")

print("=" * 68)
print("场景 5: 显式 keyword 模式 —— 会动邮件,但只在需要时")
print("=" * 68)
m5 = FakeMail(uidvalidity=b'820', supports_keywords=True)
m5.add(1)
mode5 = lm.resolve_push_mode(m5, {**CFG, 'push_mode': 'keyword'})
assert mode5 == 'keyword', mode5
assert m5.mails[1]['kw'] == set(), "探针标记必须撤销"
print(f"  ✅ 显式配置才启用 {mode5},探针已撤销\n")

print("=" * 68)
print("场景 6: keyword 但服务器不支持 -> 自动退回 watermark")
print("=" * 68)
m6 = FakeMail(uidvalidity=b'830', supports_keywords=False)
m6.add(1)
mode6 = lm.resolve_push_mode(m6, {**CFG, 'push_mode': 'keyword'})
assert mode6 == 'watermark', mode6
assert not m6.mails[1]['answered'], "绝不能降级到 answered"
print(f"  ✅ 退回 {mode6},没碰 \\Answered\n")

print("=" * 68)
print("场景 7: keyword 模式的状态恢复能力(仅供需要的人)")
print("=" * 68)
wipe_local_state()
m7 = FakeMail(uidvalidity=b'840', supports_keywords=True)
for u in (1, 2, 3):
    m7.add(u, seen=True, kw={lm.CUSTOM_PUSH_FLAG})
m7.add(4, seen=True)      # 新到,已被读过
wipe_local_state()
got = run(m7, mode='keyword')
assert got == ['4'], got
print("  ✅ 靠 keyword 精确恢复\n")

print("=" * 68)
print("场景 8: watermark 冷启动 -> 跳过历史(此模式的已知代价)")
print("=" * 68)
wipe_local_state()
m8 = FakeMail(uidvalidity=b'850')
for u in (1, 2, 3):
    m8.add(u, seen=True)
got = run(m8, mode='watermark')
assert got == [], got
print("  ✅ 跳过历史,且邮件状态没动")
m8.add(4, seen=True)
got = run(m8, mode='watermark')
assert got == ['4'], got
print("  ✅ 新邮件照推,不受已读影响\n")

print("=" * 68)
print("场景 9: limit 截断后下轮补齐")
print("=" * 68)
wipe_local_state()
m9 = FakeMail(uidvalidity=b'860')
m9.add(0)                          # 占位,先建基线(否则冷启动会跳过历史)
run(m9, mode='watermark')
for u in (1, 2, 3):
    m9.add(u, seen=True)
got = run(m9, mode='watermark', limit=2)
assert got == ['1', '2'], got
got = run(m9, mode='watermark', limit=2)
assert got == ['3'], f"❌ limit 截断漏了 3: {got}"
print("  ✅ 通过\n")

print("=" * 68)
print("场景 10: 屏蔽词仍然只在 yml 里配置")
print("=" * 68)
items = lm.parse_blocked_items('广告,推广，spam@x.com')
assert [w for w, _ in items] == ['广告', '推广', 'spam@x.com']
assert lm.match_blocked_keyword('限时推广', items) == '推广'
print(f"  ✅ 通过: {[w for w,_ in items]}\n")

print("=" * 68)
print("场景 11: 代码里没有 auto 分支残留")
print("=" * 68)
src = open(_SRC, encoding='utf-8').read()
fn = src[src.index('def resolve_push_mode'):src.index('PUSH_MODE_DESC')]
assert "auto" not in fn, "resolve_push_mode 里还有 auto 残留"
assert "def probe_custom_flag" in src
print("  ✅ 通过: 默认路径不会触发探测\n")

print("🎉 全部场景通过")
