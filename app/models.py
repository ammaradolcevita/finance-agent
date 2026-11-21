"""Shapes of what tools return (Pydantic). Shared later by our own agent (stage C) and any dashboard."""

from datetime import date

from pydantic import BaseModel, Field


class CategoryOut(BaseModel):
    id: int
    name: str
    kind: str = Field(description="regular = everyday (in the forecast); structural = counts as spending, "
                                  "not in the forecast; excluded = neither spending nor income")
    parent: str | None = None
    description: str | None = None


class AccountOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    unit: str = "EUR"


class ContextOut(BaseModel):
    today: date = Field(description="Today's date in the user's time zone. Use it for 'today', 'yesterday', etc.")
    weekday: str
    timezone: str
    accounts: list[AccountOut]
    categories: list[CategoryOut]
    notes: list[str] = Field(default_factory=list, description="Anything Claude should know before acting")
