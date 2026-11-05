# LARK-MAIL 运行镜像
#
# 为什么需要这个 Dockerfile:
#   原部署方式用 python:3.9-slim 起容器,command 里写 "pip install requests && python ...",
#   等于【每次重启都要联网拉一遍依赖】。快则十几秒,慢则卡住,PyPI 抖动时直接失败,
#   配合 restart: always 就是无限重启循环 —— 而这段时间里邮件是没人管的。
#   把依赖装进镜像后,重启是秒级的,并且完全不需要外网。
FROM python:3.9-slim

# 依赖预装(镜像层缓存,只在 Dockerfile 变化时重建)
RUN pip install --no-cache-dir "requests>=2.31,<3"

WORKDIR /app

# 代码打进镜像作为兜底版本;
# docker-compose 里仍会挂载 ./lark-mail.py 覆盖它,所以改完脚本 restart 即生效,不用 rebuild。
COPY lark-mail.py /app/lark-mail.py

# 状态文件默认落在 /data —— docker-compose 会把这个目录挂到宿主机 ./data
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UID_STATE_FILE=/data/uid_state.json

CMD ["python", "-u", "lark-mail.py"]
