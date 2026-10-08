import os
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help='Authenticate the Telethon user session used for public Telegram source collection.'
    def handle(self,*args,**kwargs):
        try:
            from telethon.sync import TelegramClient
        except ImportError:
            self.stderr.write('Telethon is required. Run pip install -r requirements.txt.'); return
        api_id=os.environ.get('TELEGRAM_API_ID'); api_hash=os.environ.get('TELEGRAM_API_HASH'); session=os.environ.get('TELEGRAM_SESSION','opportunity_hub')
        if not api_id or not api_hash:
            self.stderr.write('Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.'); return
        with TelegramClient(session,int(api_id),api_hash) as client:
            client.start()
            me=client.get_me()
            self.stdout.write(self.style.SUCCESS(f'Telegram session authenticated for {getattr(me,"username",None) or getattr(me,"first_name", "user")}'))
