import importlib.metadata
from typing import TYPE_CHECKING

from .checkpoint_store import Checkpoint
from .results import RunResult, TrialResult
from .spec import (
    Agent,
    Budget,
    Experiment,
    Files,
    Resources,
    Sandbox,
    Task,
    Validation,
    Env,
    Worker,
)
from .state import StateTracker


if TYPE_CHECKING:
    # Core classes
    from sprint.agents.base import BaseAgent
    from sprint.environments.base import BaseEnvironment, ExecResult
    from sprint.job import Job
    from sprint.job_plan import JobPlan
    from sprint.trial.hooks import LogCallback, LogEntry

    from sprint.compile import Compiler

    # Agent models
    from sprint.models.agent.context import AgentContext
    from sprint.models.agent.name import AgentName
    from sprint.bridges.base import BaseBridge
    from sprint.models.bridge import BridgeConfig, BridgeKind
    from sprint.models.dataset_item import DownloadedDatasetItem

    # Enum types
    from sprint.models.environment_type import EnvironmentType

    # Job models
    from sprint.models.job.config import (
        DatasetConfig,
        JobConfig,
        RetryConfig,
    )
    from sprint.models.job.result import AgentDatasetStats, JobResult, JobStats

    # Metric models
    from sprint.models.metric.config import MetricConfig
    from sprint.models.metric.type import MetricType
    from sprint.models.metric.usage_info import UsageInfo

    # Registry models
    from sprint.models.registry import (
        DatasetSpec,
        LocalRegistryInfo,
        Registry,
        RegistryTaskId,
        RemoteRegistryInfo,
    )

    # Task models
    from sprint.models.task.config import (
        AgentConfig,
        EnvironmentConfig,
        TaskConfig,
        VerifierConfig,
    )
    from sprint.models.task.id import GitTaskId, LocalTaskId
    from sprint.models.task.paths import TaskPaths
    from sprint.models.trial.config import (
        AgentConfig as TrialAgentConfig,
    )
    from sprint.models.trial.config import (
        EnvironmentConfig as TrialEnvironmentConfig,
    )
    from sprint.models.trial.config import (
        TaskConfig as TrialTaskConfig,
    )

    # Trial models
    from sprint.models.trial.config import (
        TrialConfig,
        UserAgentConfig,
    )
    from sprint.models.trial.config import (
        VerifierConfig as TrialVerifierConfig,
    )
    from sprint.models.trial.paths import TrialPaths
    from sprint.models.trial.result import (
        AgentInfo,
        ExceptionInfo,
        ModelInfo,
        TimingInfo,
    )

    # Verifier models
    from sprint.models.verifier.result import VerifierResult
    from sprint.trial.hooks import TrialEvent, TrialHookEvent
    from sprint.trial.queue import TrialQueue
    from sprint.trial.trial import Trial
    from sprint.verifier.base import BaseVerifier
    from sprint.verifier.verifier import Verifier

__version__ = importlib.metadata.version("sprint")


# Lazy imports to avoid loading heavy dependencies at package import time
_LAZY_IMPORTS = {
    # Core classes
    "Job": ("sprint.job", "Job"),
    "JobPlan": ("sprint.job_plan", "JobPlan"),
    "Trial": ("sprint.trial.trial", "Trial"),
    "Task": ("sprint.models.task.task", "Task"),
    "BaseAgent": ("sprint.agents.base", "BaseAgent"),
    "BaseBridge": ("sprint.bridges.base", "BaseBridge"),
    "BaseEnvironment": ("sprint.environments.base", "BaseEnvironment"),
    "ExecResult": ("sprint.environments.base", "ExecResult"),
    "BaseVerifier": ("sprint.verifier.base", "BaseVerifier"),
    "LogEntry": ("sprint.trial.hooks", "LogEntry"),
    "LogCallback": ("sprint.trial.hooks", "LogCallback"),
    "Verifier": ("sprint.verifier.verifier", "Verifier"),
    "TrialQueue": ("sprint.trial.queue", "TrialQueue"),
    "Compiler": ("sprint.compile", "Compiler"),
    # Job models
    "JobConfig": ("sprint.models.job.config", "JobConfig"),
    "RetryConfig": ("sprint.models.job.config", "RetryConfig"),
    "DatasetConfig": ("sprint.models.job.config", "DatasetConfig"),
    "LocalDatasetConfig": (
        "sprint.models.job.config",
        "DatasetConfig",
    ),  # deprecated alias
    "RegistryDatasetConfig": (
        "sprint.models.job.config",
        "DatasetConfig",
    ),  # deprecated alias
    "JobResult": ("sprint.models.job.result", "JobResult"),
    "JobStats": ("sprint.models.job.result", "JobStats"),
    "AgentDatasetStats": ("sprint.models.job.result", "AgentDatasetStats"),
    # Trial models
    "TrialConfig": ("sprint.models.trial.config", "TrialConfig"),
    "UserAgentConfig": ("sprint.models.trial.config", "UserAgentConfig"),
    "BridgeConfig": ("sprint.models.bridge", "BridgeConfig"),
    "BridgeKind": ("sprint.models.bridge", "BridgeKind"),
    "TrialAgentConfig": ("sprint.models.trial.config", "AgentConfig"),
    "TrialEnvironmentConfig": ("sprint.models.trial.config", "EnvironmentConfig"),
    "TrialVerifierConfig": ("sprint.models.trial.config", "VerifierConfig"),
    "TrialTaskConfig": ("sprint.models.trial.config", "TaskConfig"),
    "TrialResult": ("sprint.models.trial.result", "TrialResult"),
    "TimingInfo": ("sprint.models.trial.result", "TimingInfo"),
    "ExceptionInfo": ("sprint.models.trial.result", "ExceptionInfo"),
    "ModelInfo": ("sprint.models.trial.result", "ModelInfo"),
    "AgentInfo": ("sprint.models.trial.result", "AgentInfo"),
    "TrialPaths": ("sprint.models.trial.paths", "TrialPaths"),
    # Task models
    "TaskConfig": ("sprint.models.task.config", "TaskConfig"),
    "EnvironmentConfig": ("sprint.models.task.config", "EnvironmentConfig"),
    "AgentConfig": ("sprint.models.task.config", "AgentConfig"),
    "VerifierConfig": ("sprint.models.task.config", "VerifierConfig"),
    "TaskPaths": ("sprint.models.task.paths", "TaskPaths"),
    "LocalTaskId": ("sprint.models.task.id", "LocalTaskId"),
    "GitTaskId": ("sprint.models.task.id", "GitTaskId"),
    # Agent models
    "AgentContext": ("sprint.models.agent.context", "AgentContext"),
    "AgentName": ("sprint.models.agent.name", "AgentName"),
    # Verifier models
    "VerifierResult": ("sprint.models.verifier.result", "VerifierResult"),
    # Enum types
    "EnvironmentType": ("sprint.models.environment_type", "EnvironmentType"),
    "TrialEvent": ("sprint.trial.hooks", "TrialEvent"),
    "TrialHookEvent": ("sprint.trial.hooks", "TrialHookEvent"),
    # Registry models
    "LocalRegistryInfo": ("sprint.models.registry", "LocalRegistryInfo"),
    "RemoteRegistryInfo": ("sprint.models.registry", "RemoteRegistryInfo"),
    "RegistryTaskId": ("sprint.models.registry", "RegistryTaskId"),
    "DatasetSpec": ("sprint.models.registry", "DatasetSpec"),
    "Registry": ("sprint.models.registry", "Registry"),
    "DownloadedDatasetItem": ("sprint.models.dataset_item", "DownloadedDatasetItem"),
    # Metric models
    "MetricConfig": ("sprint.models.metric.config", "MetricConfig"),
    "MetricType": ("sprint.models.metric.type", "MetricType"),
    "UsageInfo": ("sprint.models.metric.usage_info", "UsageInfo"),
}


def __getattr__(name):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        import importlib

        module = importlib.import_module(module_path)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Core classes
    "Job",
    "JobPlan",
    "Trial",
    "Task",
    "BaseAgent",
    "BaseBridge",
    "BaseEnvironment",
    "ExecResult",
    "BaseVerifier",
    "LogEntry",
    "LogCallback",
    "Verifier",
    "TrialQueue",
    "Compiler",
    # Job models
    "JobConfig",
    "RetryConfig",
    "DatasetConfig",
    "LocalDatasetConfig",  # deprecated alias
    "RegistryDatasetConfig",  # deprecated alias
    "JobResult",
    "JobStats",
    "AgentDatasetStats",
    # Trial models
    "TrialConfig",
    "UserAgentConfig",
    "BridgeConfig",
    "BridgeKind",
    "TrialAgentConfig",
    "TrialEnvironmentConfig",
    "TrialVerifierConfig",
    "TrialTaskConfig",
    "TrialResult",
    "TimingInfo",
    "ExceptionInfo",
    "ModelInfo",
    "AgentInfo",
    "TrialPaths",
    # Task models
    "TaskConfig",
    "EnvironmentConfig",
    "AgentConfig",
    "VerifierConfig",
    "TaskPaths",
    "LocalTaskId",
    "GitTaskId",
    # Agent models
    "AgentContext",
    "AgentName",
    # Verifier models
    "VerifierResult",
    # Enum types
    "EnvironmentType",
    "TrialEvent",
    "TrialHookEvent",
    # Registry models
    "LocalRegistryInfo",
    "RemoteRegistryInfo",
    "RegistryTaskId",
    "DatasetSpec",
    "Registry",
    "DownloadedDatasetItem",
    # Metric models
    "MetricConfig",
    "MetricType",
    "UsageInfo",
]


__all__ = [
    "Checkpoint",
    "Agent",
    "Budget",
    "Experiment",
    "Files",
    "Resources",
    "RunResult",
    "TrialResult",
    "Sandbox",
    "StateTracker",
    "Task",
    "Validation",
    "Env",
    "Worker",
]
