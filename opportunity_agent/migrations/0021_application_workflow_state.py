from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('opportunity_agent', '0020_privateaccesstoken'),
    ]

    operations = [
        migrations.AddField(
            model_name='application',
            name='workflow_state',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
