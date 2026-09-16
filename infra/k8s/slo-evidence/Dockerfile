FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates kubernetes-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 slo-evidence \
    && useradd --system --uid 10001 --gid slo-evidence --no-create-home --home-dir /nonexistent slo-evidence

COPY infra/k8s/tools/slo-daily-evidence.py /opt/personal-server/slo-daily-evidence.py
RUN chmod 0555 /opt/personal-server/slo-daily-evidence.py

USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python3", "-B", "/opt/personal-server/slo-daily-evidence.py"]
