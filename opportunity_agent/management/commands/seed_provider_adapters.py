from django.core.management.base import BaseCommand
from opportunity_agent.models import ProviderAdapter

class Command(BaseCommand):
    help='Create safe starter adapters for known application domains. They remain disabled until an administrator configures selectors and credentials.'
    def handle(self,*args,**kwargs):
        rows=[
            ('UN Inspira','playwright_configured',['inspira.un.org','careers.un.org']),
            ('African Union Careers','playwright_configured',['jobs.au.int','au.int']),
        ]
        for name,kind,domains in rows:
            obj,created=ProviderAdapter.objects.get_or_create(name=name,defaults={'adapter_type':kind,'enabled':False,'config':{'allowed_domains':domains,'smart_fill':True,'smart_submit':True,'auto_submit':False,'login':{},'application_fields':{},'headless':True}})
            if created: self.stdout.write(self.style.SUCCESS(f'Created disabled adapter: {name}'))
            else: self.stdout.write(f'Exists: {name}')
