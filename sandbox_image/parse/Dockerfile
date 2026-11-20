# 文件解析的沙箱镜像：解析库 + LibreOffice（老式 doc/xls/ppt 转换）+ tesseract（图片和扫描页 OCR）。与 run_python 的镜像分开，那个镜像不带 soffice。
# 构建用 build.sh。运行参数（断网、只读根、限额、挂载）由 eva/sandbox/docker.py 在 docker run 时给，镜像里不固化。
# 解析器代码不打进镜像，每次运行由宿主放进工作目录，所以改解析器不用重建镜像。
FROM python:3.10-slim

# 国内机器直连官方源可能不通，构建时可用 --build-arg 换源
ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/

# 只装转换所需的 writer/calc/impress 组件；中文字体用于转换时的排版；tesseract 带中文和英文语言包
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-core libreoffice-writer libreoffice-calc \
       libreoffice-impress fonts-wqy-microhei tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX}" -r /tmp/requirements.txt

WORKDIR /work
