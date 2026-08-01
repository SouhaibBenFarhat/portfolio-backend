"""System checks for settings that name a host.

`manage.py check` runs in CI and, more importantly, in Render's build — so a check that
fails here stops a bad deploy rather than reporting it afterwards.

There is one thing to catch, and it has already happened: a URL setting keeping its
localhost default in production. `FRONTEND_URL` did exactly that, and it went unnoticed for
weeks because nothing reads it until a social login has already failed — at which point the
visitor was redirected to `http://localhost:4321/auth/error`, a page that exists only on the
developer's laptop. The failure mode of this class of bug is that it is invisible until it
matters, which is precisely what a system check is for.

The opposite mistake — a *production* host as a default, so local development quietly talks
to the live service — is guarded by a test instead. A check can't see it, because by the time
settings are imported the default and an explicit environment value look identical.
"""

from django.conf import settings
from django.core.checks import Error, register

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")


def _is_local(value: str) -> bool:
    return any(host in (value or "") for host in _LOCAL_HOSTS)


@register()
def check_urls_are_not_local_in_production(app_configs, **kwargs):
    """With DEBUG off, no setting naming a host may still point at a developer's machine."""
    if settings.DEBUG:
        return []

    errors = []
    for name in getattr(settings, "URL_SETTINGS", ()):
        value = getattr(settings, name, "")
        if _is_local(value):
            errors.append(
                Error(
                    f"{name} points at {value!r} with DEBUG off.",
                    hint=(
                        f"That is a developer's machine, so anyone it redirects reaches "
                        f"nothing. Set {name} in the host's environment (render.yaml)."
                    ),
                    id="core.E001",
                )
            )

    for name in getattr(settings, "URL_LIST_SETTINGS", ()):
        local = [entry for entry in getattr(settings, name, []) if _is_local(entry)]
        if local:
            # A warning, not an error: a local origin in an allowlist is harmless to
            # visitors — it only widens what may call the API — but it is still almost
            # always a leftover, and the deploy should say so.
            errors.append(
                Error(
                    f"{name} still allows {local} with DEBUG off.",
                    hint=(
                        "Development origins in a production allowlist are usually a "
                        f"leftover. Set {name} explicitly in the host's environment."
                    ),
                    id="core.E002",
                )
            )
    return errors
