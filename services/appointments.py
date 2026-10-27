"""Appointments service facade redirecting to core_bot.services.appointments."""

import sys
import core_bot.services.appointments as _mod

sys.modules[__name__] = _mod
