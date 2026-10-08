from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('opportunity_agent','0002_match')]
    operations = [
        migrations.AddField(model_name='opportunity',name='application_form_url',field=models.URLField(blank=True)),
        migrations.AddField(model_name='opportunity',name='application_form_type',field=models.CharField(blank=True,choices=[('web','Web Form'),('pdf','PDF Form'),('docx','DOCX Form'),('other','Other')],max_length=20)),
        migrations.CreateModel(name='SiteCredential',fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),('name',models.CharField(max_length=255)),('domain',models.CharField(db_index=True,max_length=255)),('login_url',models.URLField(blank=True)),('username',models.CharField(blank=True,max_length=255)),('encrypted_password',models.TextField(blank=True)),('encrypted_secret',models.TextField(blank=True)),('auth_type',models.CharField(choices=[('form','Username / Password Form'),('basic','HTTP Basic Auth'),('token','Token / Secret'),('other','Other')],default='form',max_length=20)),('enabled',models.BooleanField(default=True)),('metadata',models.JSONField(blank=True,default=dict)),('last_used_at',models.DateTimeField(blank=True,null=True)),('created_at',models.DateTimeField(auto_now_add=True)),('updated_at',models.DateTimeField(auto_now=True)),('user',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='site_credentials',to=settings.AUTH_USER_MODEL))],options={'indexes':[models.Index(fields=['user','domain','enabled'],name='sitecred_user_domain_idx')],'constraints':[models.UniqueConstraint(fields=('user','domain','name'),name='unique_site_credential')]},),
        migrations.CreateModel(name='ApplicationFormTemplate',fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),('name',models.CharField(max_length=255)),('domains',models.JSONField(blank=True,default=list)),('form_type',models.CharField(choices=[('pdf','PDF'),('docx','DOCX'),('web','Web Form')],default='web',max_length=20)),('download_url',models.URLField(blank=True)),('file',models.FileField(blank=True,null=True,upload_to='application_forms/')),('field_map',models.JSONField(blank=True,default=dict)),('selector_map',models.JSONField(blank=True,default=dict)),('enabled',models.BooleanField(default=True)),('notes',models.TextField(blank=True)),('created_at',models.DateTimeField(auto_now_add=True)),('updated_at',models.DateTimeField(auto_now=True))],),
        migrations.CreateModel(name='ApplicationArtifact',fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),('kind',models.CharField(max_length=40)),('file',models.FileField(upload_to='application_artifacts/')),('label',models.CharField(blank=True,max_length=255)),('created_at',models.DateTimeField(auto_now_add=True)),('application',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='artifacts',to='opportunity_agent.application'))],),
    ]
