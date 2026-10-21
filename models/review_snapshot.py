"""The latest shared group numbering, activated after delivery."""

from dataclasses import dataclass, field


@dataclass
class ReviewSnapshot:
    id: str
    expires_at: float
    items: dict[int, str] = field(default_factory=dict)
    active: bool = False
