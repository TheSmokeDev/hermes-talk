"""Phone settings and data belong to the existing Hermes Talk plugin/profile."""

from dataclasses import asdict
from pathlib import Path

from .policy import PhoneError, Settings

INJECTION_OPT_IN = (
    "To let Phone send caller input to your existing Hermes conversation, the profile owner "
    "can set this permission in the active profile:\n"
    "plugins.entries.hermes-talk.allow_gateway_injection: true"
)


class PhoneContext:
    def __init__(self, parent):
        self.parent = parent

    def get_config(self, key, default=None):
        return self.parent.get_config("phone_" + key, default=default)

    def set_config(self, key, value):
        self.parent.set_config("phone_" + key, value)

    def __getattr__(self, name):
        return getattr(self.parent, name)


def phone_context(ctx):
    return ctx if isinstance(ctx, PhoneContext) else PhoneContext(ctx)


def enabled(ctx):
    return phone_context(ctx).get_config("enabled", default=False) is True


def save_phone_settings(ctx, settings: Settings, *, enable=True):
    """Keep dialing off while replacing its allowlists and connection, even on write failure."""
    ctx = phone_context(ctx)
    values = asdict(settings)
    dialing = values.pop("dialing_enabled")
    changes = {}
    for key, value in values.items():
        value = list(value) if isinstance(value, tuple) else value
        if ctx.get_config(key) != value:
            changes[key] = value
    was_dialing = ctx.get_config("dialing_enabled", False) is True
    if was_dialing and (changes or not dialing):
        ctx.set_config("dialing_enabled", False)
    for key, value in changes.items():
        ctx.set_config(key, value)
    if dialing and (changes or not was_dialing):
        ctx.set_config("dialing_enabled", True)
    if enable and not enabled(ctx):
        ctx.set_config("enabled", True)


def require_enabled(ctx):
    if not enabled(ctx):
        raise PhoneError(
            "Phone mode is off. Enable it in Hermes Talk plugin settings or "
            "run hermes talk phone setup."
        )


def data_path() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("hermes-talk") / "phone" / "calls.db"
