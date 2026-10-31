import pytest
from widget.parser import parse_url


def test_empty():
    with pytest.raises(ValueError):
        parse_url("")
