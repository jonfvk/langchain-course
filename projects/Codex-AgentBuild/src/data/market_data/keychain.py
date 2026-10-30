"""Local OAuth storage only. No network clients or environment exports."""
import json
import sys
from dataclasses import dataclass

SERVICE = "com.codex-agentbuild.tastytrade.production.market-data"
ACCOUNT = "oauth-client-v1"
# Separate sandbox namespace; production loading never falls back to it.
SANDBOX_SERVICE = "com.codex-agentbuild.tastytrade.sandbox"


class KeychainError(RuntimeError):
    """Safe error whose message contains no backend details."""


class Secret:
    __slots__ = ("__value",)

    def __init__(self, value: str):
        if not isinstance(value, str) or not value.strip():
            raise KeychainError("All three OAuth values must be nonempty.")
        self.__value = value

    def __repr__(self):
        return "Secret([REDACTED])"

    def __str__(self):
        return "[REDACTED]"

    def __reduce_ex__(self, protocol):
        raise TypeError("Secret serialization is disabled.")

    def _for_auth(self) -> str:
        """Explicit plaintext access for the market-data auth boundary only."""
        return self.__value


@dataclass(frozen=True, slots=True)
class ProductionMarketDataAuth:
    client_id: Secret
    client_secret: Secret
    refresh_token: Secret


def _backend():
    if sys.platform != "darwin":
        raise KeychainError("macOS Keychain is required; no fallback is allowed.")
    try:
        from keyring.backends.macOS import Keyring
        backend = Keyring()  # Never use backend auto-discovery or keyring config.
        backend.keychain = None
        return backend
    except Exception:
        pass
    raise KeychainError("macOS Keychain backend unavailable; install project dependencies.")


def store_production_market_data(auth: ProductionMarketDataAuth) -> None:
    """Replace one complete bundle; JSON exists only in memory and Keychain."""
    if type(auth) is not ProductionMarketDataAuth:
        raise KeychainError("Production credential type required.")
    try:
        payload = json.dumps({"environment": "production", "credentials": {name: getattr(auth, name)._for_auth()
                              for name in ("client_id", "client_secret", "refresh_token")}})
        _backend().set_password(SERVICE, ACCOUNT, payload)
        return
    except Exception:
        pass
    # Raise outside the handler so even chained tracebacks omit backend errors.
    raise KeychainError("Keychain storage failed; no success confirmed. Rerun setup.")


def load_production_market_data() -> ProductionMarketDataAuth:
    """Retrieve at runtime; never print, export, cache, or contact tastytrade."""
    try:
        payload = _backend().get_password(SERVICE, ACCOUNT)
        bundle = json.loads(payload)
        if bundle.get("environment") != "production":
            raise ValueError
        values = bundle["credentials"]
        if set(values) != {"client_id", "client_secret", "refresh_token"}:
            raise ValueError
        return ProductionMarketDataAuth(**{k: Secret(v) for k, v in values.items()})
    except Exception:
        pass
    raise KeychainError("Production credentials NOT CONFIGURED or inaccessible; separate production setup required.")
