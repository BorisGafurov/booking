"""Masters sync service facade redirecting to core_bot.services.masters_sync."""

import sys
import core_bot.services.masters_sync as _mod

sys.modules[__name__] = _mod
