"""Referrals service facade redirecting to core_bot.services.referrals."""

import sys
import core_bot.services.referrals as _mod

sys.modules[__name__] = _mod
