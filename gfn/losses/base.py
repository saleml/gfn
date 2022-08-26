from abc import ABC, abstractmethod

from torchtyping import TensorType

from ..containers import States, Trajectories, Transitions


class Loss(ABC):
    "Abstract Base Class for all GFN Losses"

    @abstractmethod
    def __call__(self, *args, **kwargs) -> TensorType[0, float]:
        pass


class EdgeDecomposableLoss(Loss, ABC):
    @abstractmethod
    def __call__(self, edges: Transitions) -> TensorType[0, float]:
        pass


class StateDecomposableLoss(Loss, ABC):
    @abstractmethod
    def __call__(self, states: States) -> TensorType[0, float]:
        pass


class TrajectoryDecomposableLoss(Loss, ABC):
    @abstractmethod
    def __call__(self, trajectories: Trajectories) -> TensorType[0, float]:
        pass
