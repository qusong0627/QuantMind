"""Service health metrics helpers for FastAPI services."""

from fastapi import Response

try:
    from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Gauge, generate_latest

    def _create_gauge(name: str, documentation: str, labelnames=()):
        collector = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if collector is not None:
            return collector
        return Gauge(name, documentation, labelnames=labelnames)

    SERVICE_HEALTH_STATUS = _create_gauge(
        "quantmind_service_health_status",
        "Service health status (1 healthy, 0 unhealthy)",
        ["service"],
    )
    SERVICE_DEGRADED = _create_gauge(
        "quantmind_service_degraded",
        "Service degraded status (1 degraded, 0 healthy)",
        ["service"],
    )
except Exception:  # pragma: no cover - metrics is optional
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    SERVICE_HEALTH_STATUS = None
    SERVICE_DEGRADED = None

    def generate_latest() -> bytes:
        return b"# metrics unavailable\n"


# 记录各服务上一次健康状态，用于检测「健康 → 不健康」迁移并落一条系统事件
_prev_health: dict[str, bool] = {}


def set_service_health(service_name: str, healthy: bool) -> None:
    """Update service-level health gauges. 健康状态由良好翻转至不健康时，落一条系统事件。"""
    if SERVICE_HEALTH_STATUS is not None:
        SERVICE_HEALTH_STATUS.labels(service=service_name).set(1 if healthy else 0)
    if SERVICE_DEGRADED is not None:
        SERVICE_DEGRADED.labels(service=service_name).set(0 if healthy else 1)

    if not healthy:
        prev = _prev_health.get(service_name, True)
        if prev:  # 仅当此前是健康、现变不健康（或首次即不健康）时记录，避免 /health 轮询刷屏
            _prev_health[service_name] = False
            try:
                from backend.shared.system_events import record_system_event

                record_system_event(
                    event_type="health_transition",
                    level="error",
                    source=service_name,
                    title=f"服务不健康：{service_name}",
                    message=f"{service_name} 健康状态由正常变为异常 / 启动即异常",
                )
            except Exception:  # noqa: BLE001 - 事件记录非关键路径
                pass
    else:
        _prev_health[service_name] = True


def build_metrics_response() -> Response:
    """Build a Prometheus text response."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
