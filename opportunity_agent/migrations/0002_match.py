from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('opportunity_agent', '0001_initial')]
    operations = [
        migrations.CreateModel(
            name='Match',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('score', models.PositiveSmallIntegerField(default=0)),
                ('eligible', models.BooleanField(default=False)),
                ('reasons', models.JSONField(blank=True, default=list)),
                ('missing_requirements', models.JSONField(blank=True, default=list)),
                ('risk_factors', models.JSONField(blank=True, default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('opportunity', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='matches', to='opportunity_agent.opportunity')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='opportunity_matches', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-score', '-updated_at'],
            },
        ),
        migrations.AddIndex(model_name='match', index=models.Index(fields=['user', 'score'], name='opportunity_user_id_3b7a5e_idx')),
        migrations.AddIndex(model_name='match', index=models.Index(fields=['opportunity', 'score'], name='opportunity_opportu_9e0c48_idx')),
        migrations.AddIndex(model_name='match', index=models.Index(fields=['eligible', 'score'], name='opportunity_eligibl_52b9e6_idx')),
        migrations.AddConstraint(model_name='match', constraint=models.UniqueConstraint(fields=('user','opportunity'), name='unique_match_per_user_opportunity')),
    ]
