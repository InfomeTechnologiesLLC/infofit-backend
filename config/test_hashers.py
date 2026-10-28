from django.contrib.auth.hashers import PBKDF2PasswordHasher


class FastPBKDF2PasswordHasher(PBKDF2PasswordHasher):
    """Same algorithm and format as production, with few iterations so the test suite runs in seconds. Tests only."""
    iterations = 1000
