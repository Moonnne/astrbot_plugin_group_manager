"""Transport-neutral group requests and membership changes."""

from dataclasses import dataclass

from .scope import Scope


@dataclass(frozen=True)
class GroupEvent:
    scope: Scope
    kind: str
    member: str
    happened: float
    request_id: str = ""
    subtype: str = "add"
    reason: str = ""
    method: str = "申请入群"
    inviter: str = ""
    nickname: str = ""
    approved: bool = False
