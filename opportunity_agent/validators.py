import re

from django.core.exceptions import ValidationError


class NumericPasswordValidator:
    def __init__(self, min_numeric=1):
        self.min_numeric = min_numeric

    def validate(self, password, user=None):
        if sum(ch.isdigit() for ch in password) < self.min_numeric:
            raise ValidationError(
                'The password must contain at least 1 digit.',
                code='password_no_numeric',
            )

    def get_help_text(self):
        return 'Your password must contain at least 1 digit.'
