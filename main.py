import hashlib,json,logging,os,signal,sqlite3,threading,time
from datetime import datetime
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from provider import UZ,ProviderError,ROUTE_TEXT,KYIV

log=logging.getLogger('uzbot')
STOP=threading.Event()

class State:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.lock=threading.RLock()
        with self.db:self.db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    def get(self,key,default=None):
        with self.lock:
            row=self.db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else default
    def set(self,key,value):
        with self.lock,self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))

class TelegramError(Exception):
    def __init__(self,code,retry=30):
        super().__init__('Telegram HTTP '+str(code));self.retry=retry

class Telegram:
    def __init__(self,token):self.base='https://api.telegram.org/bot'+token+'/'
    def call(self,method,**data):
        req=Request(self.base+method,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
        try:
            with urlopen(req,timeout=40) as r:result=json.load(r)
        except HTTPError as e:
            try:body=json.load(e);retry=int(body.get('parameters',{}).get('retry_after',30))
            except Exception:retry=30
            raise TelegramError(e.code,retry) from None
        except (URLError,TimeoutError,OSError,ValueError):raise TelegramError('network') from None
        if not result.get('ok'):raise TelegramError(result.get('error_code','unknown'))
        return result['result']
    def send(self,chat,text):
        return self.call('sendMessage',chat_id=chat,text=text[:4000],
            link_preview_options={'is_disabled':True},reply_markup={'keyboard':[
                [{'text':'Статус'},{'text':'Проверить'}],[{'text':'Пауза'},{'text':'Продолжить'}]],'resize_keyboard':True})

class Bot:
    def __init__(self,tg,owner,state,uz,interval=120):
        self.tg,self.owner,self.s,self.uz=tg,owner,state,uz
        self.interval=max(60,interval)
        self.wake=threading.Event()
        self.s.set('status','Запуск. Данные УЗ ещё не проверены.')
    def notify(self,text):
        if self.s.get('subscribed',False):self.tg.send(self.owner,text)
    def alert(self,result):
        # Stable key includes both legs and sorted seat options; excludes volatile UZ IDs.
        canonical=[sorted(leg,key=lambda p:(p['wagon'],p['seats'])) for leg in result['matches']]
        fingerprint=hashlib.sha256(json.dumps(canonical,sort_keys=True).encode()).hexdigest()
        now=time.time()
        if fingerprint==self.s.get('fingerprint'):return
        if now-self.s.get('last_alert',0)<900:return
        lines=['Найдены места на обоих участках!','',ROUTE_TEXT,'']
        for title,items in zip(['Киев → Чоп — нижние в одном купе:','Чоп → Братислава — 2-й класс:'],result['matches']):
            lines.append(title)
            for p in items[:8]:lines.append(f"Вагон {p['wagon']}: места {p['seats'][0]} и {p['seats'][1]}")
            if len(items)>8:lines.append(f'И ещё {len(items)-8} вариантов.')
        lines+=['',f"Пересадка: {result['gap_minutes']} мин.",
                'https://booking.uz.gov.ua/','Места не забронированы; наличие может измениться.']
        self.notify('\n'.join(lines))
        self.s.set('fingerprint',fingerprint);self.s.set('last_alert',now)
    def monitor(self):
        failures=0
        while not STOP.is_set():
            self.wake.clear()
            delay=self.interval
            try:
                if datetime.now(KYIV)>=datetime(2026,10,24,20,25,tzinfo=KYIV):
                    self.s.set('status','Дата отправления наступила. Мониторинг завершён.')
                    self.s.set('paused',True)
                elif self.s.get('subscribed',False) and not self.s.get('paused',False):
                    checked=datetime.now(KYIV).strftime('%d.%m %H:%M:%S')
                    result=self.uz.check()
                    self.s.set('status',result['message'])
                    self.s.set('last_success',checked)
                    self.s.set('last_error',None)
                    failures=0
                    if result['complete'] and not self.s.get('paused',False) and not STOP.is_set():self.alert(result)
                    elif not result.get('partial'):self.s.set('fingerprint',None)
            except ProviderError as e:
                failures+=1
                delay=max(e.retry,min(3600,self.interval*2**min(failures,5)))
                message=str(e)
                self.s.set('status','Проверка не выполнена: '+message)
                if e.auth:self.s.set('paused',True)
                try:
                    if self.s.get('last_error')!=message:
                        self.notify('Мониторинг: '+message+ ('\nПоиск на паузе. После обновления Variables нажми «Продолжить».' if e.auth else ''))
                        self.s.set('last_error',message)
                except TelegramError:pass
                log.warning('UZ check failed: %s',message)
            except TelegramError as e:
                delay=max(self.interval,e.retry)
                log.warning('Telegram delivery failed; retry later')
            except Exception:
                # Do not log exception text/tracebacks: response/session data may contain secrets.
                self.s.set('status','Ошибка обработки данных УЗ. Подходящие места не подтверждены; проверь формат API.')
                delay=600
                log.error('Unexpected provider/schema error; see status, no credentials logged')
                try:
                    if self.s.get('last_error')!='schema':
                        self.notify(self.s.get('status'));self.s.set('last_error','schema')
                except TelegramError:pass
            self.s.set('next_check',int(time.time()+delay))
            end=time.monotonic()+delay
            while not STOP.is_set() and time.monotonic()<end:
                if self.wake.wait(min(1,max(0,end-time.monotonic()))):
                    # Manual checks respect at least the normal interval AND backoff.
                    self.wake.clear()
    def command(self,msg):
        if msg.get('chat',{}).get('type')!='private' or msg.get('from',{}).get('id')!=self.owner or msg['chat'].get('id')!=self.owner:return
        cmd=msg.get('text','').split('@')[0].strip()
        if cmd in ('/start','/help'):
            self.s.set('subscribed',True)
            text='Личный мониторинг УЗ.\n'+ROUTE_TEXT+'\n\n'+self.status()
        elif cmd in ('Статус','/status'):text=self.status()
        elif cmd in ('Пауза','/pause'):
            self.s.set('paused',True);text='Поиск приостановлен.'
        elif cmd in ('Продолжить','/resume'):
            self.s.set('paused',False);self.s.set('subscribed',True)
            text='Поиск включён. Следующая проверка — по расписанию, с учётом ограничений УЗ.'
        elif cmd in ('Проверить','/check'):
            text=self.status()+'\n\nСледующая проверка выполняется по расписанию. Частые нажатия не увеличивают нагрузку на УЗ.'
        else:text='Используй кнопки «Статус», «Проверить», «Пауза», «Продолжить».'
        self.tg.send(self.owner,text)
    def status(self):
        return ('Режим: '+('пауза' if self.s.get('paused',False) else 'включён')+'\n'+self.s.get('status','Ожидание')+
            '\nПоследний успешный ответ УЗ: '+str(self.s.get('last_success','ещё не получен'))+
            '\nИнтервал: '+str(self.interval)+' с. При ошибках интервал увеличивается.')
    def run(self):
        # Dedicated bot only. Existing webhook is removed explicitly for long polling.
        self.tg.call('deleteWebhook',drop_pending_updates=False)
        worker=threading.Thread(target=self.monitor,daemon=True);worker.start()
        while not STOP.is_set():
            try:
                updates=self.tg.call('getUpdates',offset=self.s.get('offset',0),timeout=25,allowed_updates=['message'])
                for update in updates:
                    if 'message' in update:self.command(update['message'])
                    self.s.set('offset',update['update_id']+1)
            except TelegramError as e:
                log.warning('Telegram polling failed; token omitted from logs')
                STOP.wait(max(5,e.retry))
        worker.join(timeout=35)

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
    owner=os.getenv('ALLOWED_USER_ID','')
    if not token or not owner.isdigit():raise SystemExit('Set TELEGRAM_BOT_TOKEN and numeric ALLOWED_USER_ID.')
    state=State(str(Path(os.getenv('DATA_DIR','/data'))/'state.sqlite3'))
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:STOP.set())
    Bot(Telegram(token),int(owner),state,UZ(),int(os.getenv('POLL_INTERVAL_SECONDS','120'))).run()
if __name__=='__main__':main()
