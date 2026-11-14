"""Phone settings and data belong to the existing Hermes Talk plugin/profile."""

from pathlib import Path

from .policy import PhoneError


class PhoneContext:
    def __init__(self, parent):
        self.parent = parent

    def get_config(self, key, default=None):
        return self.parent.get_config("phone_" + key, default=default)

    def __getattr__(self, name):
        return getattr(self.parent, name)


def phone_context(ctx):
    return ctx if isinstance(ctx, PhoneContext) else PhoneContext(ctx)


def enabled(ctx):
    return phone_context(ctx).get_config("enabled", default=False) is True


def require_enabled(ctx):
    if not enabled(ctx):
        raise PhoneError(
            "Phone mode is off. Enable it in Hermes Talk plugin settings or "
            "run hermes talk phone setup."
        )


def data_path() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("hermes-talk") / "phone" / "calls.db"
