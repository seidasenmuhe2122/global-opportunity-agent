import os, asyncio
from django.core.management.base import BaseCommand
from opportunity_agent.models import Application, Opportunity, Source
from opportunity_agent.tasks import automation_cycle_task
class Command(BaseCommand):
    help='Run the authorized Opportunity Hub Telegram admin bot.'
    def handle(self,*args,**kwargs):
        token=os.environ.get('TELEGRAM_BOT_TOKEN',''); allowed={x.strip() for x in os.environ.get('TELEGRAM_ADMIN_CHAT_IDS','').split(',') if x.strip()}
        if not token: self.stderr.write('TELEGRAM_BOT_TOKEN is required.'); return
        try: from telegram import Update
        except ImportError: self.stderr.write('python-telegram-bot is required.'); return
        from telegram.ext import ApplicationBuilder,CommandHandler,ContextTypes
        async def guard(update): return str(update.effective_chat.id) in allowed if allowed else False
        async def status(update,context):
            if not await guard(update): return
            await update.message.reply_text(f'Users: {__import__("django").contrib.auth.get_user_model().objects.count()}\nOpportunities: {Opportunity.objects.filter(status="active").count()}\nApplications: {Application.objects.count()}\nSources: {Source.objects.filter(enabled=True).count()}')
        async def scan(update,context):
            if not await guard(update): return
            result=automation_cycle_task.delay(); await update.message.reply_text(f'Automation cycle queued: {result.id}')
        async def help_cmd(update,context):
            if await guard(update): await update.message.reply_text('/status — platform metrics\n/scan — queue automation cycle\n/sources — source count\n/opportunities — active opportunity count\n/applications — application count\n/help — commands')
        async def sources(update,context):
            if await guard(update): await update.message.reply_text(f'Enabled sources: {Source.objects.filter(enabled=True).count()}')
        async def opportunities(update,context):
            if await guard(update): await update.message.reply_text(f'Active opportunities: {Opportunity.objects.filter(status="active").count()}')
        async def applications(update,context):
            if await guard(update): await update.message.reply_text(f'Applications: {Application.objects.count()} | submitted: {Application.objects.filter(status="submitted").count()} | review: {Application.objects.filter(status="needs_review").count()}')
        app=ApplicationBuilder().token(token).build(); app.add_handler(CommandHandler('status',status)); app.add_handler(CommandHandler('scan',scan)); app.add_handler(CommandHandler('help',help_cmd)); app.add_handler(CommandHandler('sources',sources)); app.add_handler(CommandHandler('opportunities',opportunities)); app.add_handler(CommandHandler('applications',applications)); self.stdout.write('Telegram admin bot is running.'); app.run_polling(drop_pending_updates=True)
