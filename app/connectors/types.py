"""Small value types shared across connectors, independent of BaseConnector.

Split out from base.py: CredentialStatus is used by steam_connector.py
(a plain module of functions, no BaseConnector subclass — see its own
docstring) as much as by BaseConnector's check_credentials(), so it
doesn't actually belong to that interface module.
"""

from dataclasses import dataclass


@dataclass
class CredentialStatus:
    ok: bool
    message: str = ""
