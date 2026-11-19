FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY wq_publish ./wq_publish
COPY wsgi.py ./

# 数据目录与下游目录默认分开，容器内由两个独立卷分别挂载（不同挂载点）
ENV WQ_DATA_DIR=/data \
    WQ_DOWNSTREAM_DIR=/downstream

EXPOSE 5000

CMD ["waitress-serve", "--host", "0.0.0.0", "--port", "5000", "--call", "wsgi:create_app"]
