from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('opportunity_agent', '0024_opportunity_application_method'),
    ]

    operations = [
        migrations.AddField(
            model_name='opportunity',
            name='application_methods',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='opportunity',
            name='application_instructions',
            field=models.TextField(blank=True),
        ),
    ]
