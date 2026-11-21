"""Pure logic: the name resolver and date handling. No database."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.dates import NOON, check_when, in_words, resolve_when
from app.services.resolve import Candidate, resolve

C = [Candidate(1, "Sunny Bakery", ("SB",)), Candidate(2, "Corner Market"), Candidate(3, "Corner Market Express"),
     Candidate(4, "Bus")]
AMS = ZoneInfo("Europe/Amsterdam")


# --- resolver ---------------------------------------------------------------------

def test_exact_ignores_case_and_spaces():
    m = resolve("  sunny   BAKERY ", C)
    assert (m.kind, m.candidate.id) == ("exact", 1)


def test_alias():
    m = resolve("sb", C)
    assert (m.kind, m.candidate.id) == ("alias", 1)


def test_obvious_typo_is_fuzzy():
    m = resolve("Sunny Bakry", C)
    assert (m.kind, m.candidate.id) == ("fuzzy", 1)


def test_two_equally_close_names_are_never_auto_picked():
    twins = [Candidate(1, "Market North"), Candidate(2, "Market South")]
    m = resolve("Market Nouth", twins)   # one letter away from both
    assert m.kind == "none"
    assert set(m.suggestions) == {"Market North", "Market South"}


def test_short_words_are_never_guessed():
    assert resolve("bsu", C).kind == "none"


def test_no_fuzzy_when_not_allowed():
    m = resolve("Sunny Bakry", C, allow_fuzzy=False)
    assert m.kind == "none"
    assert "Sunny Bakery" in m.suggestions


def test_unknown_gives_suggestions_not_guesses():
    m = resolve("Bakery", C)
    assert m.kind == "none"
    assert m.suggestions and m.suggestions[0] == "Sunny Bakery"


# --- dates -------------------------------------------------------------------------

NOW = datetime(2026, 12, 10, 15, 30, tzinfo=AMS)


def test_no_date_means_now_exactly():
    when, exact = resolve_when(None, None, AMS, NOW)
    assert when == NOW and exact


def test_date_without_time_is_noon_local():
    when, exact = resolve_when("2026-12-09", None, AMS, NOW)
    assert when.time() == NOON and when.tzinfo == AMS and not exact


def test_noon_survives_the_summer_time_change():
    when, _ = resolve_when("2026-03-29", None, AMS, NOW)   # clocks go forward that night
    assert when.astimezone(AMS).date().isoformat() == "2026-03-29"
    assert when.astimezone(AMS).hour == 12


def test_future_is_an_error_and_old_is_a_warning():
    assert check_when(NOW + timedelta(days=1), NOW)[0]
    assert check_when(NOW - timedelta(days=400), NOW)[1]
    assert check_when(NOW - timedelta(days=1), NOW) == (None, None)


def test_in_words():
    when, exact = resolve_when("2026-12-03", None, AMS, NOW)
    assert in_words(when, AMS, exact) == "Thu 3 Dec 2026"
    when, exact = resolve_when("2026-12-03", "08:15", AMS, NOW)
    assert in_words(when, AMS, exact) == "Thu 3 Dec 2026, 08:15"
