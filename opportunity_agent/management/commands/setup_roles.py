from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db import transaction


ROLE_PERMISSIONS = {
    'ADMIN': [
        'auth.add_user', 'auth.change_user', 'auth.delete_user', 'auth.view_user',
        'opportunity_agent.add_userprofile', 'opportunity_agent.change_userprofile',
        'opportunity_agent.view_userprofile',
        'opportunity_agent.add_source', 'opportunity_agent.change_source',
        'opportunity_agent.delete_source', 'opportunity_agent.scan_sources',
        'opportunity_agent.view_source',
        'opportunity_agent.add_telegramsource',
        'opportunity_agent.change_telegramsource',
        'opportunity_agent.delete_telegramsource',
        'opportunity_agent.scan_telegram_sources',
        'opportunity_agent.view_telegramsource',
        'opportunity_agent.add_privateaccesstoken',
        'opportunity_agent.change_privateaccesstoken',
        'opportunity_agent.view_privateaccesstoken',
        'opportunity_agent.add_opportunity', 'opportunity_agent.change_opportunity',
        'opportunity_agent.delete_opportunity', 'opportunity_agent.view_opportunity',
        'opportunity_agent.add_application', 'opportunity_agent.change_application',
        'opportunity_agent.delete_application', 'opportunity_agent.review_application',
        'opportunity_agent.retry_application', 'opportunity_agent.view_application',
        'opportunity_agent.view_applicationattempt', 'opportunity_agent.view_match',
        'opportunity_agent.add_telegramdestination',
        'opportunity_agent.change_telegramdestination',
        'opportunity_agent.delete_telegramdestination',
        'opportunity_agent.manage_telegram',
        'opportunity_agent.view_telegramdestination',
        'opportunity_agent.add_provideradapter',
        'opportunity_agent.change_provideradapter',
        'opportunity_agent.delete_provideradapter',
        'opportunity_agent.manage_automation',
        'opportunity_agent.view_provideradapter',
        'opportunity_agent.view_automationrun',
        'opportunity_agent.view_auditlog',
        'opportunity_agent.add_aiconversation',
        'opportunity_agent.change_aiconversation',
        'opportunity_agent.view_aiconversation',
        'opportunity_agent.add_aimessage',
        'opportunity_agent.view_aimessage',
    ],
    'OPERATOR': [
        'opportunity_agent.add_source', 'opportunity_agent.change_source',
        'opportunity_agent.delete_source', 'opportunity_agent.scan_sources',
        'opportunity_agent.view_source',
        'opportunity_agent.add_telegramsource',
        'opportunity_agent.change_telegramsource',
        'opportunity_agent.delete_telegramsource',
        'opportunity_agent.scan_telegram_sources',
        'opportunity_agent.view_telegramsource',
        'opportunity_agent.add_opportunity', 'opportunity_agent.change_opportunity',
        'opportunity_agent.delete_opportunity', 'opportunity_agent.view_opportunity',
        'opportunity_agent.change_application', 'opportunity_agent.retry_application',
        'opportunity_agent.view_application', 'opportunity_agent.view_applicationattempt',
        'opportunity_agent.view_automationrun',
        'opportunity_agent.add_aiconversation',
        'opportunity_agent.change_aiconversation',
        'opportunity_agent.view_aiconversation',
        'opportunity_agent.add_aimessage',
        'opportunity_agent.view_aimessage',
    ],
    'REVIEWER': [
        'opportunity_agent.review_application',
        'opportunity_agent.view_application',
        'opportunity_agent.view_applicationattempt',
        'opportunity_agent.add_aiconversation',
        'opportunity_agent.change_aiconversation',
        'opportunity_agent.view_aiconversation',
        'opportunity_agent.add_aimessage',
        'opportunity_agent.view_aimessage',
    ],
    'USER': [
        'opportunity_agent.view_userprofile', 'opportunity_agent.change_userprofile',
        'opportunity_agent.view_opportunity',
        'opportunity_agent.add_application', 'opportunity_agent.change_application',
        'opportunity_agent.view_application', 'opportunity_agent.view_match',
        'opportunity_agent.add_match',
        'opportunity_agent.add_emailmailbox', 'opportunity_agent.change_emailmailbox',
        'opportunity_agent.delete_emailmailbox', 'opportunity_agent.view_emailmailbox',
        'opportunity_agent.add_sitecredential', 'opportunity_agent.change_sitecredential',
        'opportunity_agent.delete_sitecredential', 'opportunity_agent.view_sitecredential',
        'opportunity_agent.add_aiconversation',
        'opportunity_agent.change_aiconversation',
        'opportunity_agent.view_aiconversation',
        'opportunity_agent.add_aimessage',
        'opportunity_agent.view_aimessage',
    ],
}


def _all_super_admin_permissions():
    return Permission.objects.filter(
        content_type__app_label__in=('admin', 'auth', 'opportunity_agent'),
    ).distinct()


def _permissions_for_role(name):
    if name == 'SUPER ADMIN':
        return _all_super_admin_permissions()

    codenames = ROLE_PERMISSIONS[name]
    permissions = []
    for value in codenames:
        app_label, codename = value.split('.', 1)
        try:
            permissions.append(Permission.objects.get(
                content_type__app_label=app_label,
                codename=codename,
            ))
        except Permission.DoesNotExist as exc:
            raise RuntimeError(
                f'Missing permission {value}; run migrations before setting up roles.'
            ) from exc
    return permissions


@transaction.atomic
def ensure_group(name):
    group, _ = Group.objects.get_or_create(name=name)
    group.permissions.set(_permissions_for_role(name))
    return group


class Command(BaseCommand):
    help = 'Synchronize platform groups and least-privilege permissions.'

    def handle(self, *args, **options):
        role_groups = {
            name: ensure_group(name)
            for name in ('SUPER ADMIN', *ROLE_PERMISSIONS)
        }
        user_model = get_user_model()
        user_group = role_groups['USER']
        users = user_model._default_manager.filter(
            is_staff=False,
            is_superuser=False,
        ).exclude(groups=user_group)
        for user in users.iterator():
            user.groups.add(user_group)
        self.stdout.write(self.style.SUCCESS('Platform roles and permissions synchronized.'))
