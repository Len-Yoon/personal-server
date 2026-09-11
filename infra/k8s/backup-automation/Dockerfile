FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

RUN apt-get update \
    && apt-get install --no-install-recommends -y age bash ca-certificates coreutils findutils kubernetes-client rclone sqlite3 tar util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 portal-backup \
    && useradd --system --uid 10001 --gid portal-backup --no-create-home --home-dir /nonexistent portal-backup

COPY infra/k8s/tools/portal-pvc-backup-verify.sh /opt/personal-server/portal-pvc-backup-verify.sh
COPY infra/k8s/tools/validate-backup-evidence.py /opt/personal-server/validate-backup-evidence.py
RUN chmod 0555 /opt/personal-server/portal-pvc-backup-verify.sh /opt/personal-server/validate-backup-evidence.py

USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/opt/personal-server/portal-pvc-backup-verify.sh", "--go"]
