"""recommendations service facade redirecting to core_bot.services.recommendations."""

import sys
import core_bot.services.recommendations as _mod

sys.modules[__name__] = _mod
