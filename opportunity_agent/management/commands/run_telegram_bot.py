from django.core.management.base import BaseCommand
from opportunity_agent.services.telegram_bot import build_application_bot
class Command(BaseCommand):
    help='Run the authorized Telegram administration bot.'
    def handle(self,*args,**options):
        bot=build_application_bot(); self.stdout.write(self.style.SUCCESS('Telegram admin bot is running.')); bot.run_polling(drop_pending_updates=True)
