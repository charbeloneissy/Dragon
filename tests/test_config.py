import pytest
from dragon.config import Config

def test_live_and_dry_run_cannot_both_be_enabled():
    with pytest.raises(ValueError): Config(dry_run=True, live_trading=True).validate()
