"""Masters service facade redirecting to core_bot.services.masters."""

import sys
import core_bot.services.masters as _mod

sys.modules[__name__] = _mod
