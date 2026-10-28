"""Changes specialist agent correlating deployments and configuration changes."""

from typing import Any
from .change import ChangeSpecialist


class ChangesSpecialist(ChangeSpecialist):
    """Specialist correlating deployments and configuration updates with alert onset."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


__all__ = ["ChangesSpecialist"]
