from typing import override
from sprint.agents.base import BaseAgent
from sprint.agents.capabilities import AgentCapabilities
from sprint.environments.base import BaseEnvironment
from sprint.models.agent.context import AgentContext
from sprint.models.agent.name import AgentName


class NopAgent(BaseAgent):
    capabilities = AgentCapabilities(windows=True)

    @staticmethod
    @override
    def name() -> str:
        return AgentName.NOP.value

    @override
    def version(self) -> str:
        return "1.0.0"

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        pass
