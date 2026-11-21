"""The resolver: turns what the user typed into one of their categories or accounts.

Order: exact name (ignoring case and extra spaces) → alias → obvious typo (fuzzy) → suggestions.
It never guesses silently: a fuzzy pick is only allowed where the caller shows it as a warning (in a preview),
and only when there is one clear winner. Pure function, no database.
"""

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

AUTO_PICK_SCORE = 0.85     # "Albert Hein" vs "Albert Heijn" ≈ 0.96
CLEAR_LEAD = 0.10          # the winner must beat the runner-up by this much
SUGGEST_SCORE = 0.5
MIN_LEN_FOR_FUZZY = 4      # never auto-pick for very short words like "ah"


@dataclass(frozen=True)
class Candidate:
    id: int
    name: str
    aliases: tuple[str, ...] = ()


@dataclass
class Match:
    kind: Literal["exact", "alias", "fuzzy", "none"]
    candidate: Candidate | None = None
    score: float = 0.0
    suggestions: list[str] = field(default_factory=list)


def normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _score(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def resolve(text: str, candidates: list[Candidate], allow_fuzzy: bool = True) -> Match:
    wanted = normalize(text)
    for c in candidates:
        if normalize(c.name) == wanted:
            return Match("exact", c, 1.0)
    for c in candidates:
        if any(normalize(a) == wanted for a in c.aliases):
            return Match("alias", c, 1.0)

    scored = sorted(
        ((max(_score(wanted, normalize(n)) for n in (c.name, *c.aliases)), c) for c in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    suggestions = [c.name for s, c in scored[:3] if s >= SUGGEST_SCORE]
    if allow_fuzzy and scored and len(wanted) >= MIN_LEN_FOR_FUZZY:
        best_score, best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if best_score >= AUTO_PICK_SCORE and best_score - runner_up >= CLEAR_LEAD:
            return Match("fuzzy", best, best_score, suggestions)
    return Match("none", None, scored[0][0] if scored else 0.0, suggestions)
