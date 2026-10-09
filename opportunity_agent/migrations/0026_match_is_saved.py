from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('opportunity_agent', '0025_opportunity_application_routes'),
    ]

    operations = [
        migrations.AddField(
            model_name='match',
            name='is_saved',
            field=models.BooleanField(default=False),
        ),
    ]
