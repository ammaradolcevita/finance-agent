"""Errors that are the user's or the model's to fix (unknown category, missing amount…).

The message always says what to do next, so Claude can recover by itself.
Anything else is an internal error: logged with a reference id, shown without details.
"""


class UserError(Exception):
    def __init__(self, code: str, message: str, suggestions: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestions = suggestions or []

    def for_model(self) -> str:
        text = f"{self.code}: {self.message}"
        if self.suggestions:
            text += " Suggestions: " + ", ".join(self.suggestions)
        return text
