"""One applicant and the cached outcome of its approval."""

from dataclasses import dataclass

from .scope import Scope


@dataclass
class JoinRequest:
    id: str
    scope: Scope
    qq: str
    flag: str
    subtype: str
    reason: str
    received_at: float
    expires_at: float
    inviter: str = ""
    status: str = "pending"
    actor: str = ""
    decided_at: float | None = None
    method: str = "申请入群"
