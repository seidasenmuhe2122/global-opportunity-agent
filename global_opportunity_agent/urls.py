from django.contrib import admin
from django.urls import include, path

admin.site.site_header = 'Opportunity Hub • Command Center'
admin.site.site_title = 'Opportunity Hub'
admin.site.index_title = 'Automation & Intelligence Center'
admin.site.index_template = 'admin/index.html'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('opportunity_agent.urls')),
]
