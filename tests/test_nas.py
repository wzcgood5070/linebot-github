from datetime import datetime

import pytest

from app.nas import LocalNas, build_nas, safe_name


def test_safe_name_uses_meeting_month_and_day():
    assert safe_name("ignored title", datetime(2026, 9, 7, 14, 30)) == "0907"


def test_build_nas_local():
    assert isinstance(build_nas("local"), LocalNas)


def test_build_nas_rejects_unknown_driver():
    with pytest.raises(ValueError, match="Unsupported NAS_DRIVER"):
        build_nas("typo")
