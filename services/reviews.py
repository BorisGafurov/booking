"""Reviews service facade redirecting to core_bot.services.reviews."""

import sys
import core_bot.services.reviews as _mod

sys.modules[__name__] = _mod
