"""Derivation of a service's config-override alias from its interface path.

Shared between the WM and ER (ADR-0064): WM clients like CLI parse override names out of
environment variables and CLI input, the ER matches them against the bindings in
its DI registry. Both sides must derive the same string from the same interface,
so the rule lives in one place.

See ADR-0070 for why the alias is always derived and never declared.
"""

import re

__all__ = ["derive_service_name"]

# Split before a capital that starts a new word: either after a lowercase/digit
# (`HttpClient` -> `Http|Client`) or at the end of a run of capitals
# (`HTTPClient` -> `HTTP|Client`). A naive `(?=[A-Z])` would render `IHTTPClient`
# as `h_t_t_p_client`, which nobody would guess when writing the env var.
_SNAKE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def derive_service_name(interface: str) -> str:
    """Derive the config-override alias from a service's interface path.

    Takes the interface's final dotted segment (the class name for Python
    interfaces), strips a leading ``I`` when followed by an uppercase letter
    (the common interface-prefix convention), and snake-cases the result.

    Examples:
        ``finecode_extension_api.interfaces.ihttpclient.IHttpClient``
            -> ``http_client``
        ``...irepositorycredentialsprovider.IRepositoryCredentialsProvider``
            -> ``repository_credentials_provider``
        ``fine_tasks.IForgeCredentialsProvider`` -> ``forge_credentials_provider``
    """
    last_segment = interface.rsplit(".", 1)[-1]
    if len(last_segment) > 1 and last_segment[0] == "I" and last_segment[1].isupper():
        last_segment = last_segment[1:]
    return _SNAKE_BOUNDARY.sub("_", last_segment).lower()
