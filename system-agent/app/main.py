import os

from fastapi import FastAPI

from app.services.metrics import collect_metrics, demo_metrics


app = FastAPI(title="Personal Server System Agent")


@app.get("/health")
def health():
    return {
        "service": "system-agent",
        "status": "ok",
    }


@app.get("/metrics")
def metrics():
    if _truthy(os.getenv("DEMO_MODE", "")):
        return demo_metrics()
    return collect_metrics()


@app.get("/metrics/demo")
def metrics_demo():
    return demo_metrics()


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
