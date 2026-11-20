#!/bin/sh
# 用法：sandbox_image/parse/build.sh [镜像名]，在仓库任意位置执行均可。
# 镜像名要与 configs/parsing.yaml 的 sandbox.image 一致。
set -e
IMAGE="${1:-eva-parse:latest}"
HERE="$(cd "$(dirname "$0")" && pwd)"
docker build -t "$IMAGE" "$HERE"
