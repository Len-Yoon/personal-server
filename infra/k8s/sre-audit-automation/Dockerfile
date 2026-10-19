FROM python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929

RUN apk upgrade --no-cache \
    && apk add --no-cache bash ca-certificates coreutils kubectl tzdata \
    && pip install --no-cache-dir setuptools==84.0.0 wheel==0.48.0 \
    && addgroup -S -g 10001 sre-audit \
    && adduser -S -u 10001 -G sre-audit -H -h /nonexistent sre-audit

COPY infra/k8s/tools/quarterly-sre-audit-runner.sh /opt/personal-server/quarterly-sre-audit-runner.sh
COPY infra/k8s/tools/validate-backup-evidence.py /opt/personal-server/validate-backup-evidence.py
RUN chmod 0555 /opt/personal-server/quarterly-sre-audit-runner.sh /opt/personal-server/validate-backup-evidence.py

USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["/opt/personal-server/quarterly-sre-audit-runner.sh"]
