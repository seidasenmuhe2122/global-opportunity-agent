from ..models import AuditLog


def record_audit_event(action, target='', details=None, actor=None):
    if details is not None and not isinstance(details, dict):
        raise TypeError('Audit event details must be a dictionary.')
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        target=str(target)[:255],
        details=details or {},
    )
