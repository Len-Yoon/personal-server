FROM python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929

RUN apk upgrade --no-cache \
    && apk add --no-cache age ca-certificates kubectl rclone \
    && ln -s /bin/cat /usr/bin/cat \
    && pip install --no-cache-dir setuptools==84.0.0 wheel==0.48.0 \
    && addgroup -S -g 10001 book-backup \
    && adduser -S -u 10001 -G book-backup -H -h /nonexistent book-backup

COPY infra/k8s/tools/book-pvc-backup-verify.py /opt/personal-server/book-pvc-backup-verify.py
COPY infra/k8s/tools/pvc_backup_retention.py /opt/personal-server/pvc_backup_retention.py
RUN chmod 0555 /opt/personal-server/book-pvc-backup-verify.py

USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/opt/personal-server/book-pvc-backup-verify.py", "--go"]
