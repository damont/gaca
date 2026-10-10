"""Pick how gaca-api reaches the Hermes machine for a given user."""
from __future__ import annotations

from ..config import settings
from ..models.user import UserDoc
from .base import Provisioner
from .local import LocalHermes
from .ssh import SshProvisioner


def provisioner_for(user: UserDoc) -> Provisioner | None:
    """`local` mode: always available, no per-user credential involved. `ssh` mode: only once
    the user has supplied a connection, else None ("gaca-api has no access to the box yet")."""
    if settings.agent_access == "local":
        return LocalHermes(settings.hermes_home, settings.hermes_bin)
    if user.ssh is not None:
        return SshProvisioner(user.ssh)
    return None
