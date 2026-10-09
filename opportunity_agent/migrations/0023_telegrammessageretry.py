from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('opportunity_agent', '0022_source_candidate_scan_cursor_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='TelegramMessageRetry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('channel_identifier', models.CharField(max_length=255)),
                ('message_id', models.PositiveBigIntegerField()),
                ('message_text', models.TextField()),
                ('retry_count', models.PositiveSmallIntegerField(default=1)),
                ('next_retry_at', models.DateTimeField(blank=True, null=True)),
                ('status', models.CharField(choices=[('pending', 'Pending retry'), ('resolved', 'Resolved'), ('dead_letter', 'Dead letter / manual review')], default='pending', max_length=20)),
                ('last_error', models.TextField(blank=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('source', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='message_retries', to='opportunity_agent.telegramsource')),
            ],
        ),
        migrations.AddConstraint(
            model_name='telegrammessageretry',
            constraint=models.UniqueConstraint(fields=('source', 'message_id'), name='unique_telegram_source_message_retry'),
        ),
        migrations.AddIndex(
            model_name='telegrammessageretry',
            index=models.Index(fields=['status', 'next_retry_at'], name='opportunity_status_d4c39d_idx'),
        ),
        migrations.AddIndex(
            model_name='telegrammessageretry',
            index=models.Index(fields=['source', 'status'], name='opportunity_source__c82a6d_idx'),
        ),
    ]
