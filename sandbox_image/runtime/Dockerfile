# run_python 的沙箱镜像：只有解释器、数据处理和文档读写库，不含主服务任何依赖。
# 构建用 build.sh。运行参数（断网、只读根、限额、挂载）由 eva/sandbox/docker.py 在 docker run 时给，镜像里不固化。
FROM python:3.10-slim

# 国内机器直连官方源可能不通，构建时可用 --build-arg 换源
ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/

# 中文字体：matplotlib 出图用 wqy-microhei；报告（WeasyPrint 出 PDF）用 Noto Sans CJK，有粗体。
# pango/harfbuzz/fontconfig 是 WeasyPrint 的系统依赖。字体缓存在构建时生成：运行时根文件系统只读，
# 不预先生成的话每次运行都要重新扫描一遍字体
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-microhei fonts-noto-cjk \
       libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libfontconfig1 \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX}" -r /tmp/requirements.txt

# 根文件系统运行时只读，工作目录和 /tmp 由 docker run 挂载；不设 ENTRYPOINT，命令由宿主给
WORKDIR /work
