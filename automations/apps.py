from django.apps import AppConfig


class AutomationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "automations"

    def ready(self):
        from django.conf import settings
        from django.db.models.signals import post_save

        post_save.connect(_create_settings, sender=settings.AUTH_USER_MODEL,
                          dispatch_uid="automations_settings_for_new_user")


def _create_settings(sender, instance, created, raw=False, **kwargs):
    """Every new account starts with coach messages on (each can be turned off in the app's Settings)."""
    if created and not raw:
        from .models import AutomationSettings
        AutomationSettings.objects.get_or_create(user=instance)
