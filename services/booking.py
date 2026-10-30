"""Booking service facade redirecting to core_bot.services.booking."""

import sys
import core_bot.services.booking as _mod

sys.modules[__name__] = _mod
