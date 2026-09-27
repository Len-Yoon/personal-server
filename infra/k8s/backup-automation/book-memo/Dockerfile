FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

RUN apt-get update \
    && apt-get install --no-install-recommends -y age ca-certificates kubernetes-client rclone \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 book-backup \
    && useradd --system --uid 10001 --gid book-backup --no-create-home --home-dir /nonexistent book-backup

COPY infra/k8s/tools/book-pvc-backup-verify.py /opt/personal-server/book-pvc-backup-verify.py
COPY infra/k8s/tools/pvc_backup_retention.py /opt/personal-server/pvc_backup_retention.py
RUN chmod 0555 /opt/personal-server/book-pvc-backup-verify.py

USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/opt/personal-server/book-pvc-backup-verify.py", "--go"]
