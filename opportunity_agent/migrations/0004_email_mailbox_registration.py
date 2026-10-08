from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[('opportunity_agent','0003_credentials_forms')]
    operations=[
        migrations.CreateModel(
            name='EmailMailbox',
            fields=[
                ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
                ('name',models.CharField(max_length=255)),
                ('email',models.EmailField(max_length=254)),
                ('encrypted_app_password',models.TextField(blank=True)),
                ('imap_host',models.CharField(blank=True,max_length=255)),
                ('imap_port',models.PositiveIntegerField(default=993)),
                ('imap_ssl',models.BooleanField(default=True)),
                ('enabled',models.BooleanField(default=True)),
                ('last_checked_at',models.DateTimeField(blank=True,null=True)),
                ('last_error',models.TextField(blank=True)),
                ('created_at',models.DateTimeField(auto_now_add=True)),
                ('updated_at',models.DateTimeField(auto_now=True)),
                ('user',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='email_mailboxes',to=settings.AUTH_USER_MODEL)),
            ],
            options={'indexes':[models.Index(fields=['user','enabled'],name='emailbox_user_enabled_idx')],'constraints':[models.UniqueConstraint(fields=('user','email','name'),name='unique_email_mailbox')]}
        ),
        migrations.AddField(model_name='sitecredential',name='registration_url',field=models.URLField(blank=True)),
        migrations.AddField(model_name='sitecredential',name='auto_register',field=models.BooleanField(default=False)),
        migrations.AddField(model_name='sitecredential',name='email_mailbox',field=models.ForeignKey(blank=True,null=True,on_delete=django.db.models.deletion.SET_NULL,related_name='site_credentials',to='opportunity_agent.emailmailbox')),
        migrations.AddField(model_name='sitecredential',name='account_status',field=models.CharField(choices=[('not_configured','Not configured'),('registration_pending','Registration pending'),('verification_pending','Verification pending'),('ready','Ready'),('failed','Failed'),('disabled','Disabled')],default='not_configured',max_length=30)),
        migrations.AddField(model_name='sitecredential',name='registration_error',field=models.TextField(blank=True)),
        migrations.AddField(model_name='sitecredential',name='last_registration_at',field=models.DateTimeField(blank=True,null=True)),
    ]
