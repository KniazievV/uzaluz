"""Read-only UZ client. Endpoint/schema inspected in public frontend 2026-10-06.
Authenticated end-to-end validation still required; no booking/holding calls.
"""
import json, os, time, re
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlencode, quote
from urllib.error import HTTPError, URLError

class ProviderError(Exception):
    def __init__(self, message, retry=300, auth=False):
        super().__init__(message); self.retry=retry; self.auth=auth

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None

BASE='https://app.uz.gov.ua/api'
KYIV=ZoneInfo('Europe/Kyiv')
ROUTE_TEXT=('027К: Киев → Чоп, 24.10.2026 20:25 → 25.10 07:46; '
            'два нижних в одном купе.\n961К: Чоп → Братислава главная, '
            '25.10.2026 09:59 → 19:44; два места 2-го класса в одном вагоне.')

def norm(s): return re.sub(r'[^\w]', '', str(s).casefold())
def train_number(s):
    return str(s).upper().replace('K','К').lstrip('0')

# Only verified standard layouts; never infer lower berths for unknown wagons.
LAYOUTS={'К01':36, 'К22':40, 'К54':36, 'К56':40}
NAMES={'Купейний вагон 36 місць':36,'Купейний вагон 40 місць':40,
       'К54 Трансформер купейний з місцями для лежання  36 місць':36,
       'Трансформер купейний 40/4':40}

def seat_numbers(w):
    raw=w.get('seats')
    if not isinstance(raw,list): raise ProviderError('УЗ: неизвестный формат списка мест.')
    out=set()
    for n in raw:
        if isinstance(n,bool) or not str(n).isdigit():
            raise ProviderError('УЗ: неизвестный формат номера места.')
        out.add(int(n))
    return out

def pairs(wagons, lower):
    if not isinstance(wagons,list): raise ProviderError('УЗ: неизвестный формат вагонов.')
    result=[]; unknown=[]
    for w in wagons:
        if not isinstance(w,dict) or not str(w.get('number','')).isdigit():
            raise ProviderError('УЗ: отсутствует номер вагона.')
        seats=seat_numbers(w)
        if not seats: continue
        # Special seats may impose eligibility restrictions. Do not advertise them.
        if w.get('special_seats_info'):
            unknown.append('специальные места вагона '+str(w['number'])); continue
        if lower:
            key=w.get('wagon_key')
            count=LAYOUTS.get(key) if key else NAMES.get(w.get('mockup_name'))
            if count is None:
                unknown.append(str(w['number'])+': '+str(key or w.get('mockup_name','?')));continue
            if any(n<1 or n>count for n in seats):
                raise ProviderError('УЗ: нумерация не соответствует схеме вагона.')
            candidates=[(n,n+2) for n in range(1,count,4) if {n,n+2}<=seats]
        else:
            # Same wagon is required, adjacency is not assumed from numbering.
            candidates=[tuple(sorted(seats)[:2])] if len(seats)>=2 else []
        for a,b in candidates:
            result.append({'wagon':str(w['number']),'seats':[a,b]})
    return result,unknown

class UZ:
    def __init__(self):
        self.token=os.getenv('UZ_TOKEN','').strip().removeprefix('Bearer ')
        self.session=os.getenv('UZ_SESSION_ID','').strip()
        self.uid=os.getenv('UZ_USER_ID','').strip()
        self.ids={};self.last_request=0
        self.opener=build_opener(NoRedirect())
    def get(self,path,params=None):
        if not all([self.token,self.session,self.uid]):
            raise ProviderError('Нужны UZ_TOKEN, UZ_SESSION_ID и UZ_USER_ID в Railway.',auth=True)
        time.sleep(max(0,2-(time.monotonic()-self.last_request)))
        self.last_request=time.monotonic()
        url=BASE+path+('?' + urlencode(params) if params else '')
        req=Request(url,headers={'Accept':'application/json','Authorization':'Bearer '+self.token,
            'X-Session-ID':self.session,'X-User-Agent':'UZ/2 Web/1 User/'+self.uid,
            'X-Client-Locale':'uk','Origin':'https://booking.uz.gov.ua',
            'Referer':'https://booking.uz.gov.ua/'})
        try:
            with self.opener.open(req,timeout=25) as r:
                data=r.read(4_000_001)
                if len(data)>4_000_000:raise ProviderError('Слишком большой ответ УЗ.')
                return json.loads(data)
        except HTTPError as e:
            if e.code in (401,403,441,443):
                raise ProviderError(f'УЗ HTTP {e.code}: войди на сайт и обнови сессию; при необходимости пройди проверку на сайте.',auth=True) from None
            if e.code==429:
                try:delay=max(60,int(e.headers.get('Retry-After','300')))
                except ValueError:delay=300
                raise ProviderError('УЗ ограничила частоту запросов. Поиск временно приостановлен.',retry=delay) from None
            raise ProviderError(f'УЗ HTTP {e.code}. Данные не получены.') from None
        except (URLError,TimeoutError,OSError): raise ProviderError('УЗ: ошибка соединения; повтор будет позже.') from None
        except (ValueError,TypeError):raise ProviderError('УЗ: ответ не является ожидаемым JSON.') from None
    def station(self,key,query,names):
        if key in self.ids:return self.ids[key]
        forced=os.getenv('UZ_'+key+'_ID')
        if forced:
            if not forced.isdigit():raise ProviderError('Неверный ID станции '+key)
            self.ids[key]=int(forced);return int(forced)
        rows=self.get('/stations',{'search':query})
        if not isinstance(rows,list):raise ProviderError('УЗ: неизвестный формат поиска станций.')
        valid=[r for r in rows if isinstance(r,dict) and norm(r.get('name')) in {norm(n) for n in names}]
        if len(valid)!=1:
            hints=', '.join(str(r.get('name'))+'='+str(r.get('id')) for r in rows if isinstance(r,dict))
            raise ProviderError('Нужно уточнить UZ_'+key+'_ID. Найдены: '+hints[:500])
        self.ids[key]=int(valid[0]['id']);return self.ids[key]
    def find(self,a,b,date,number,clock):
        data=self.get('/v3/trips',{'station_from_id':a,'station_to_id':b,'date':date,'with_transfers':0})
        if not isinstance(data,dict) or not isinstance(data.get('direct'),list):
            raise ProviderError('УЗ: изменился формат поиска поездов.')
        matching=[t for t in data['direct'] if train_number(t.get('train',{}).get('number'))==train_number(number)]
        exact=[]
        for t in matching:
            depart=datetime.fromtimestamp(t['depart_at'],KYIV)
            if depart.strftime('%Y-%m-%d %H:%M')==date+' '+clock:exact.append(t)
        if len(exact)>1:raise ProviderError('УЗ вернула неоднозначный рейс '+number)
        if not exact and matching:raise ProviderError('УЗ: время поезда '+number+' отличается от заданного. Проверь расписание.')
        return exact[0] if exact else None
    def options(self,trip,lower):
        classes=trip['train']['wagon_classes']
        if not isinstance(classes,list):raise ProviderError('УЗ: неизвестный формат классов.')
        selected=[]
        for c in classes:
            name=norm(c.get('name',''))
            if (lower and (c.get('id')=='К' or name=='купе')) or (not lower and name in {'2клас','2класс','2class','secondclass','2ndclass'}):
                selected.append(c)
        if not selected:raise ProviderError('УЗ: требуемый класс не найден в ответе для '+trip['train']['number'])
        results=[];unknown=[]
        for c in selected:
            data=self.get('/v3/trips/'+quote(str(trip['id']),safe='')+'/wagons-by-class/'+quote(str(c['id']),safe=''))
            if not isinstance(data,dict) or 'wagons' not in data:raise ProviderError('УЗ: изменился формат списка вагонов.')
            found,skipped=pairs(data['wagons'],lower)
            results+=found;unknown+=skipped
        return results,unknown
    def check(self):
        a=self.station('KYIV','Київ',['Київ-Пас','Київ-Пасажирський'])
        b=self.station('CHOP','Чоп',['Чоп'])
        c=self.station('BRATISLAVA','Братисл',['Братисл Гол','Братислава-Головна','Bratislava hl.st.','Bratislava hlavná stanica'])
        first=self.find(a,b,'2026-10-24','027К','20:25')
        second=self.find(b,c,'2026-10-25','961К','09:59')
        if not first or not second:return {'complete':False,'message':'Один из заданных рейсов отсутствует в выдаче УЗ. Возможно, продажа ещё не открыта.','matches':[]}
        if datetime.fromtimestamp(first['arrive_at'],KYIV).strftime('%Y-%m-%d %H:%M')!='2026-10-25 07:46':
            raise ProviderError('Изменилось прибытие 027К в Чоп; проверь расписание.')
        if datetime.fromtimestamp(second['arrive_at'],ZoneInfo('Europe/Bratislava')).strftime('%Y-%m-%d %H:%M')!='2026-10-25 19:44':
            raise ProviderError('Изменилось прибытие 961К в Братиславу; проверь расписание.')
        gap=second['depart_at']-first['arrive_at']
        if gap<3600 or gap>6*3600:raise ProviderError('Расписание изменилось: пересадка вне допустимого интервала 1–6 часов.')
        one,unknown1=self.options(first,True)
        two,unknown2=self.options(second,False)
        note=''
        if unknown1+unknown2:note='\nНе проверены схемы/ограничения: '+', '.join(unknown1+unknown2)[:600]
        matches=[one,two] if one and two else []
        return {'complete':bool(matches),'matches':matches,'message':f'Киев → Чоп: {len(one)} подходящих пар. Чоп → Братислава: {len(two)} подходящих вариантов.'+note,
                'gap_minutes':int(gap/60),'partial':bool(unknown1+unknown2)}
