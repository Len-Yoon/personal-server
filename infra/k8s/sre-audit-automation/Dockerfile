FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

RUN apt-get update \
    && apt-get install --no-install-recommends -y bash ca-certificates coreutils kubernetes-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 sre-audit \
    && useradd --system --uid 10001 --gid sre-audit --no-create-home --home-dir /nonexistent sre-audit

COPY infra/k8s/tools/quarterly-sre-audit-runner.sh /opt/personal-server/quarterly-sre-audit-runner.sh
RUN chmod 0555 /opt/personal-server/quarterly-sre-audit-runner.sh

USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["/opt/personal-server/quarterly-sre-audit-runner.sh"]
