from django.core.management.base import BaseCommand
from opportunity_agent.models import ProviderAdapter
class Command(BaseCommand):
    help='Create common provider adapter templates. They remain unverified until explicitly enabled for submission.'
    def handle(self,*args,**options):
        items=[
            ('Generic HTTPS Form','generic_form',['example.com']),
            ('Greenhouse','greenhouse',['boards.greenhouse.io']),
            ('Lever','lever',['jobs.lever.co']),
            ('Workable','workable',['apply.workable.com']),
        ]
        for name,kind,domains in items:
            ProviderAdapter.objects.get_or_create(name=name,defaults={'adapter_type':kind,'enabled':False,'config':{'domains':domains,'verified':False,'allow_submit':False,'manual_mfa_required':True}})
        self.stdout.write(self.style.SUCCESS('Provider adapter templates created. Verify each domain before enabling auto-submit.'))
