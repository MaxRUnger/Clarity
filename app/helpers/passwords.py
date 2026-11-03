"""Shared password rules for signup and password reset."""

COMMON_PASSWORD_WORDS = frozenset({
    "password",
    "passw0rd",
    "welcome",
    "letmein",
    "qwerty",
    "qwertyuiop",
    "admin",
    "administrator",
    "iloveyou",
    "changeme",
    "clarity",
    "claritygrader",
    "palomar",
    "instructor",
    "professor",
    "teacher",
})


def password_error(password, email):
    """Return the first failure message, or None when the password is acceptable."""
    text = password if isinstance(password, str) else ""
    if len(text) < 12:
        return "Use at least 12 characters."
    if len(text.encode("utf-8")) > 72:
        return "Use at most 72 characters."
    if not any(ch.isalpha() and ch.islower() for ch in text):
        return "Use at least one lowercase letter."
    if not any(ch.isalpha() and ch.isupper() for ch in text):
        return "Use at least one uppercase letter."
    if not any(_is_ascii_digit(ch) for ch in text):
        return "Use at least one digit."
    if not any(_is_symbol(ch) for ch in text):
        return "Use at least one symbol such as ! or #."
    account = (email or "").strip().casefold()
    if account and text.strip().casefold() == account:
        return "Do not use your email address as the password."
    local = account.split("@", 1)[0] if "@" in account else ""
    if len(local) >= 4 and local in text.casefold():
        return "Do not include the start of your email address."
    if _base_word(text) in COMMON_PASSWORD_WORDS:
        return "Do not use a common word as the password."
    return None


def _is_ascii_digit(ch):
    return "0" <= ch <= "9"


def _is_symbol(ch):
    return bool(ch) and (not ch.isalpha()) and (not _is_ascii_digit(ch)) and (not ch.isspace())


def _base_word(password):
    text = password.casefold()
    start = 0
    end = len(text)
    while start < end and (_is_ascii_digit(text[start]) or _is_symbol(text[start])):
        start += 1
    while end > start and (_is_ascii_digit(text[end - 1]) or _is_symbol(text[end - 1])):
        end -= 1
    return text[start:end]
