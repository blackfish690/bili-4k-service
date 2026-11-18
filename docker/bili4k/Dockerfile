# syntax=docker/dockerfile:1

FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deps /install /usr/local

WORKDIR /app
COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

# 构建期自检 1/2：模块与 proto stub 必须齐全。
# 上传不完整时在这里直接 build 失败，而不是容器启动后抛 ImportError 反复重启。
RUN python -c "import pathlib,sys; base=pathlib.Path('app'); need=['__init__.py','main.py','config.py','auth.py','bili.py','mpd.py','store.py','gen/bilibili/metadata_pb2.py','gen/bilibili/metadata_pb2_grpc.py','gen/bilibili/playershared_pb2.py','gen/bilibili/app/playerunite/v1_pb2.py','gen/bilibili/app/playerunite/v1_pb2_grpc.py']; missing=[n for n in need if not (base/n).is_file()]; print('app/:', sorted(p.name for p in base.iterdir())); sys.exit('build aborted: 缺少 %s，请完整上传 outputs/backend/app 后再构建' % missing if missing else 0)"

# 构建期自检 2/2：真实 import 一次（含全部 proto stub），顺带验证依赖齐备、
# stub 与 grpcio/protobuf 版本兼容。
RUN python -c "import app.main, bilibili.metadata_pb2_grpc, bilibili.playershared_pb2_grpc, bilibili.metadata.device_pb2_grpc, bilibili.metadata.fawkes_pb2_grpc, bilibili.metadata.locale_pb2_grpc, bilibili.metadata.network_pb2_grpc; print('preflight ok: app.main + all stubs imported')"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3).getcode()==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
