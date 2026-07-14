"""Helpers for config-driven domain collection policy."""
from typing import Any, Iterable, Optional, Tuple


def host_matches_domain(host: str, domain: str) -> bool:
    domain = (domain or "").lower().lstrip(".")
    host = (host or "").lower()
    return bool(domain) and (host == domain or host.endswith(f".{domain}"))


def _entry_domains(entry: Any) -> Iterable[str]:
    if isinstance(entry, str):
        yield entry
        return
    if not isinstance(entry, dict):
        return
    for domain in entry.get("domains") or [entry.get("domain")]:
        if domain:
            yield domain


def find_unsupported_domain(host: str, policies: Iterable[Any]) -> Optional[Tuple[Any, str]]:
    for entry in policies or []:
        for domain in _entry_domains(entry):
            if host_matches_domain(host, domain):
                return entry, domain
    return None


def format_unsupported_domain_error(host: str, entry: Any, domain: str, action: str) -> str:
    if isinstance(entry, dict):
        message = entry.get("message")
        if message:
            return message.format(host=host, domain=domain, action=action)
        reason = entry.get("reason")
        if reason:
            return f"{host} is unsupported for {action}: {reason}"
    return f"{host} is unsupported for {action}."
