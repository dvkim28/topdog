"""Log in by email instead of username.

Registration (`index.forms.EmailRegistrationForm`) sets `User.username` to
the email address, but writing a dedicated backend - rather than just relying
on that convention - means login also works for the built-in superuser and
for accounts created any other way, as long as their email field is set.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        User = get_user_model()
        email = username or kwargs.get("email")
        if not email or not password:
            return None
        try:
            user = User.objects.get(email__iexact=email)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
