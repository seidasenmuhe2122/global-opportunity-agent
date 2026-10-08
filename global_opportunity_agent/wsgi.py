import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'global_opportunity_agent.settings')
application = get_wsgi_application()
