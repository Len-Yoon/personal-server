FROM python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929

RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates \
    && pip install --no-cache-dir setuptools==84.0.0 wheel==0.48.0 \
    && addgroup -S -g 10001 observations \
    && adduser -S -u 10001 -G observations -H -h /nonexistent observations

COPY infra/k8s/tools/slo-sample-summary.py /opt/observations/slo-sample-summary.py
COPY infra/k8s/tools/slo-observation-archive.py /opt/observations/slo-observation-archive.py
USER 10001:10001
WORKDIR /tmp
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "-B", "/opt/observations/slo-observation-archive.py"]
