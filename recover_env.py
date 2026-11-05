# -*- coding: utf-8 -*-
"""从容器里抢救环境变量。

场景: 误把 .env 覆盖成了 .env.example,但容器还在跑。
容器创建时注入的环境变量是快照,只要容器没被删除(rm/down 删容器)就能捞回来。

用法:
    python recover_env.py            # 默认容器名 lark-mail
    python recover_env.py 容器名

输出:
    .env.recovered  —— 恢复出来的配置(含明文授权码,用完请删除)
"""
import json
import subprocess
import sys

CONTAINER = sys.argv[1] if len(sys.argv) > 1 else 'lark-mail'

# Docker 基础镜像自带、跟我们配置无关的变量
SKIP = {
    'PATH', 'HOSTNAME', 'HOME', 'LANG', 'GPG_KEY',
    'PYTHON_VERSION', 'PYTHON_SHA256', 'PYTHON_PIP_VERSION',
    'PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE',
    'PWD', 'TERM', 'SHLVL', '_', 'OLDPWD',
}
SKIP_PREFIX = ('LC_',)


def run_inspect(name):
    try:
        out = subprocess.run(
            ['docker', 'inspect', name],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
    except FileNotFoundError:
        print('❌ 找不到 docker 命令,请确认在能访问 Docker 的机器上运行')
        sys.exit(1)
    if out.returncode != 0:
        print(f'❌ docker inspect 失败: {out.stderr.strip()}')
        print('   → 先用 docker ps -a 确认容器名和容器是否还存在')
        sys.exit(1)
    try:
        return json.loads(out.stdout)[0]
    except Exception as e:
        print(f'❌ 解析 docker inspect 输出失败: {e}')
        sys.exit(1)


def mask(key, value):
    """日志里把敏感值打码"""
    if any(s in key.upper() for s in ('PASS', 'WEBHOOK', 'SECRET', 'TOKEN', 'KEY')):
        if len(value) <= 8:
            return '***'
        return value[:6] + '***' + value[-4:]
    return value


def quote_if_needed(value):
    """值里含 $ # 空格 或引号时,用单引号包起来(docker compose 单引号内不插值)"""
    if value == '':
        return ''
    if any(c in value for c in ('$', '#', ' ', '"', "'", '\\')):
        return "'" + value.replace("'", "'\"'\"'") + "'"
    return value


def main():
    info = run_inspect(CONTAINER)
    name = info.get('Name', CONTAINER).lstrip('/')
    status = info.get('State', {}).get('Status', 'unknown')
    created = info.get('Created', '')[:19].replace('T', ' ')

    env_list = info.get('Config', {}).get('Env') or []
    print(f'📦 容器: {name}  状态: {status}  创建于: {created}')
    print(f'   共 {len(env_list)} 个环境变量,过滤基础镜像自带的之后:')
    print()

    pairs = {}
    for item in env_list:
        key, _, value = item.partition('=')
        if not key:
            continue
        if key in SKIP or key.startswith(SKIP_PREFIX):
            continue
        pairs[key] = value

    if not pairs:
        print('⚠️  没有捞到任何业务变量。可能这个容器不是用 docker-compose 启动的,')
        print('   或者容器已经被删除重建过了。')
        sys.exit(1)

    for key in sorted(pairs):
        print(f'   {key} = {mask(key, pairs[key])}')

    with open('.env.recovered', 'w', encoding='utf-8') as f:
        f.write('# 由 recover_env.py 从容器快照恢复\n')
        f.write(f'# 容器 {name},创建于 {created}\n')
        f.write('# ⚠️ 含明文凭据,确认 .env 恢复后请删除本文件\n\n')
        for key in sorted(pairs):
            f.write(f'{key}={quote_if_needed(pairs[key])}\n')

    print()
    print(f'✅ 已写入 .env.recovered({len(pairs)} 个变量)')
    print()
    print('下一步:')
    print('   1. 打开 .env.recovered 核对账号/授权码/Webhook 是否正确')
    print('   2. 把它另存为 .env(或按需挑选字段复制过去)')
    print('   3. 确认无误后删除 .env.recovered')


if __name__ == '__main__':
    main()
