from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailOrUsernameModelBackend(ModelBackend):
    def user_can_authenticate(self, user):
        if not super().user_can_authenticate(user):
            return False
        profile = getattr(user, 'profile', None)
        if profile is not None and profile.registration_status in {'pending', 'rejected', 'suspended'}:
            return False
        return True

    def authenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()
        identifier = username or kwargs.get(user_model.USERNAME_FIELD)
        if not identifier or password is None:
            return None

        user = None
        if '@' in identifier:
            matches = user_model._default_manager.filter(email__iexact=identifier)
            if matches.count() > 1:
                return None
            user = matches.first()
        else:
            user = user_model._default_manager.filter(username__iexact=identifier).first()

        if user is None or not user.check_password(password):
            return None

        if not self.user_can_authenticate(user):
            return None

        user.backend = f'{self.__module__}.{self.__class__.__name__}'
        return user
