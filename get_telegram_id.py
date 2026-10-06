"""Run locally BEFORE Railway deployment. Never prints the bot token."""
from getpass import getpass
from main import Telegram,TelegramError
print('Сначала напиши /start своему боту. Останови другие экземпляры этого бота.')
token=getpass('Токен BotFather (ввод скрыт): ').strip()
try:
    tg=Telegram(token)
    rows=tg.call('getUpdates',timeout=0,allowed_updates=['message'])
    found={}
    for row in rows:
        m=row.get('message',{})
        if m.get('chat',{}).get('type')=='private':
            u=m.get('from',{});found[u.get('id')]=u.get('username') or u.get('first_name')
    if not found: print('Сообщений нет. Отправь /start ещё раз и повтори. Проверь, что бот не запущен где-то ещё.')
    for uid,name in found.items():print(f'{name}: ALLOWED_USER_ID={uid}')
except TelegramError as e:print(str(e))
