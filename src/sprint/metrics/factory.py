from typing import Any

from sprint.metrics.base import BaseMetric
from sprint.metrics.max import Max
from sprint.metrics.mean import Mean
from sprint.metrics.min import Min
from sprint.metrics.sum import Sum
from sprint.metrics.uv_script import UvScript
from sprint.models.metric.type import MetricType


class MetricFactory:
    _METRICS: list[type[BaseMetric[Any]]] = [
        Sum,
        Min,
        Max,
        Mean,
        UvScript,
    ]
    _METRIC_MAP: dict[MetricType, type[BaseMetric[Any]]] = {
        MetricType.SUM: Sum,
        MetricType.MIN: Min,
        MetricType.MAX: Max,
        MetricType.MEAN: Mean,
        MetricType.UV_SCRIPT: UvScript,
    }

    @classmethod
    def create_metric(
        cls,
        metric_type: MetricType,
        **kwargs,
    ) -> BaseMetric[Any]:
        """
        Create a metric from a metric type.

        Args:
            metric_type (MetricType): The type of the metric.
            **kwargs: Additional keyword arguments to pass to the metric constructor.

        Returns:
            BaseMetric: The created metric.

        Raises:
            ValueError: If the metric type is invalid or required parameters are missing.
        """
        if metric_type not in cls._METRIC_MAP:
            raise ValueError(
                f"Unsupported metric type: {metric_type}. This could be because the "
                "metric is not registered in the MetricFactory or because the metric "
                "type is invalid."
            )

        return cls._METRIC_MAP[metric_type](**kwargs)
