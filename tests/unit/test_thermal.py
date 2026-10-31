from nomi.thermal import hottest_celsius, wait_until_cool


def test_cool_chip_never_waits():
    assert wait_until_cool(85, read=lambda: 60.0, sleep=lambda s: None) == 0.0


def test_unknown_temperature_never_blocks():
    assert wait_until_cool(85, read=lambda: None, sleep=lambda s: None) == 0.0


def test_waits_until_below_resume_threshold():
    readings = iter([92.0, 90.0, 84.0, 76.0, 74.0])
    slept: list[float] = []
    notes: list[str] = []
    waited = wait_until_cool(
        85, read=lambda: next(readings), sleep=slept.append, poll_s=5, notify=notes.append
    )
    assert waited == 20.0 and len(slept) == 4  # resumes only below 75 °C
    assert notes and "92" in notes[0]


def test_gives_up_on_a_stuck_sensor():
    waited = wait_until_cool(85, read=lambda: 99.0, sleep=lambda s: None, poll_s=10, max_wait_s=30)
    assert waited == 30.0


def test_reading_is_a_float_or_none():
    value = hottest_celsius()
    assert value is None or 0 < value < 150
