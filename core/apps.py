from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # Importing the module is what registers the checks — @register() runs at import.
        from . import checks  # noqa: F401
