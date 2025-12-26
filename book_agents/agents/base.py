from abc import ABC, abstractmethod
from book_agents.domain.types import Envelope

class Agent(ABC):
    name: str

    @property
    @abstractmethod
    def subscriptions(self) -> set[str]:
        raise NotImplementedError

    @abstractmethod
    async def handle(self, env: Envelope) -> list[Envelope]:
        raise NotImplementedError
