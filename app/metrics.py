from __future__ import annotations

from contextlib import suppress

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
except ImportError:  # pragma: no cover
    class _Metric:
        def labels(self, **_: str) -> "_Metric":
            return self

        def inc(self, amount: float = 1.0) -> None:
            return None

        def dec(self, amount: float = 1.0) -> None:
            return None

        def observe(self, value: float) -> None:
            return None

        def set(self, value: float) -> None:
            return None

    def Counter(*args, **kwargs):  # type: ignore[misc]
        return _Metric()

    def Gauge(*args, **kwargs):  # type: ignore[misc]
        return _Metric()

    def Histogram(*args, **kwargs):  # type: ignore[misc]
        return _Metric()

    def start_http_server(*args, **kwargs) -> None:  # type: ignore[misc]
        return None


jobs_submitted = Counter("content_forge_jobs_submitted_total", "Total jobs submitted")
jobs_completed = Counter("content_forge_jobs_completed_total", "Total jobs completed")
jobs_failed = Counter("content_forge_jobs_failed_total", "Total jobs failed", ["reason"])
jobs_duplicate = Counter("content_forge_jobs_duplicate_total", "Total duplicate urls")

job_duration = Histogram(
    "content_forge_job_duration_seconds",
    "Job processing duration in seconds",
    buckets=[30, 60, 120, 180, 300, 600],
)
token_usage = Histogram(
    "content_forge_tokens_used",
    "Tokens used per agent",
    ["agent"],
    buckets=[100, 500, 1000, 2000, 4000, 8000],
)
qa_score = Histogram(
    "content_forge_qa_score",
    "QA overall score",
    buckets=[5, 6, 7, 7.5, 8, 9, 10],
)
active_jobs = Gauge("content_forge_active_jobs", "Number of active jobs")
dlq_size = Gauge("content_forge_dlq_size", "Dead letter queue size")

_METRICS_STARTED = False


def start_metrics_server_once(port: int) -> None:
    global _METRICS_STARTED
    if _METRICS_STARTED:
        return
    with suppress(OSError):
        start_http_server(port)
        _METRICS_STARTED = True


def record_tokens(agent: str, total_tokens: int) -> None:
    token_usage.labels(agent=agent).observe(total_tokens)
