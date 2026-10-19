FROM python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929

RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates kubectl tzdata \
    && pip install --no-cache-dir setuptools==84.0.0 wheel==0.48.0 \
    && addgroup -S -g 10001 slo-evidence \
    && adduser -S -u 10001 -G slo-evidence -H -h /nonexistent slo-evidence

COPY infra/k8s/tools/slo-daily-evidence.py /opt/personal-server/slo-daily-evidence.py
RUN chmod 0555 /opt/personal-server/slo-daily-evidence.py

USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python3", "-B", "/opt/personal-server/slo-daily-evidence.py"]
