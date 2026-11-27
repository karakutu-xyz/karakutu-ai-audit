"""Environment defaults and persisted local overrides for archive intake only."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PollValues:
    polling_enabled: bool
    interval_minutes: int
    lookback_days: int
    auto_audit_enabled: bool
    auto_audit_max_per_poll: int


class PollControls:
    def __init__(self, settings, store):
        self.defaults = asdict(PollValues(
            polling_enabled=settings.poll_interval_minutes > 0,
            # Zero disables scheduling by default; an enabled schedule needs a duration.
            interval_minutes=settings.poll_interval_minutes or 5,
            lookback_days=settings.poll_lookback_days,
            auto_audit_enabled=settings.auto_audit_enabled,
            auto_audit_max_per_poll=settings.auto_audit_max_per_poll,
        ))
        self.overrides = store.poll_overrides()

    @property
    def effective(self):
        return PollValues(**{**self.defaults, **self.overrides})

    def describe(self):
        values = asdict(self.effective)
        return {name: {"value": values[name], "default": default,
                       "source": "ui" if name in self.overrides else "env"}
                for name, default in self.defaults.items()}

    def apply(self, values, reset=None):
        if reset is not None:
            self.overrides = {name: value for name, value in self.overrides.items()
                              if reset and name not in reset}
        self.overrides.update(values)
