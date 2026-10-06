import tempfile,unittest
from datetime import datetime
from unittest.mock import patch
from provider import pairs,ProviderError,UZ,KYIV
from main import Bot,State

def wagon(seats,key='К01',number='3',**extra):
    return {'number':number,'wagon_key':key,'seats':seats,**extra}
class Filtering(unittest.TestCase):
    def test_same_compartment(self):
        self.assertEqual(pairs([wagon([1,3,4,5,7])],True)[0],
            [{'wagon':'3','seats':[1,3]},{'wagon':'3','seats':[5,7]}])
    def test_two_lower_different_compartments_rejected(self):
        self.assertEqual(pairs([wagon([3,5])],True)[0],[])
    def test_upper_rejected(self):self.assertEqual(pairs([wagon([2,4])],True)[0],[])
    def test_across_wagons_rejected(self):
        self.assertEqual(pairs([wagon([1],number='1'),wagon([3],number='2')],True)[0],[])
    def test_unknown_layout_not_guessed(self):
        result,unknown=pairs([wagon([1,3],key='UNSEEN')],True)
        self.assertEqual(result,[]);self.assertTrue(unknown)
    def test_second_class_same_wagon(self):
        self.assertEqual(pairs([wagon([8,23],key='other')],False)[0],[{'wagon':'3','seats':[8,23]}])
    def test_second_class_across_wagons_rejected(self):
        self.assertEqual(pairs([wagon([1],number='1'),wagon([2],number='2')],False)[0],[])
    def test_restricted_seats_not_advertised(self):
        self.assertFalse(pairs([wagon([1,3],special_seats_info={'gender':'women'})],True)[0])
    def test_invalid_schema(self):
        for seats in [None,[{'number':1}],[True]]:
            with self.assertRaises(ProviderError):pairs([wagon(seats)],True)
    def test_out_of_range(self):
        with self.assertRaises(ProviderError):pairs([wagon([37,39])],True)
    def test_40_seat_layout(self):
        self.assertEqual(pairs([wagon([37,39],key='К22')],True)[0][0]['seats'],[37,39])

class FakeTelegram:
    def __init__(self):self.sent=[]
    def send(self,owner,text):self.sent.append((owner,text))

class StateAndDelivery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=self.temp.name+'/db'
        self.s=State(self.path);self.s.set('subscribed',True)
        self.t=FakeTelegram();self.b=Bot(self.t,123,self.s,None)
        self.r={'matches':[[{'wagon':'1','seats':[1,3]}],[{'wagon':'7','seats':[11,12]}]],'gap_minutes':133}
    def tearDown(self):self.s.db.close();self.temp.cleanup()
    def test_dedup_persists_after_restart(self):
        self.b.alert(self.r);self.b.alert(self.r);self.assertEqual(len(self.t.sent),1)
        other=State(self.path)
        Bot(self.t,123,other,None).alert(self.r)
        self.assertEqual(len(self.t.sent),1);other.db.close()
    def test_failed_send_not_marked_delivered(self):
        with patch.object(self.t,'send',side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):self.b.alert(self.r)
        self.assertIsNone(self.s.get('fingerprint'))
    def test_unauthorized_user_no_reply(self):
        self.b.command({'chat':{'type':'private','id':456},'from':{'id':456},'text':'/start'})
        self.assertEqual(self.t.sent,[])
    def test_group_no_reply(self):
        self.b.command({'chat':{'type':'group','id':-1},'from':{'id':123},'text':'/start'})
        self.assertEqual(self.t.sent,[])
    def test_pause_persists(self):
        self.b.command({'chat':{'type':'private','id':123},'from':{'id':123},'text':'/pause'})
        other=State(self.path);self.assertTrue(other.get('paused'));other.db.close()

class ApiContract(unittest.TestCase):
    def test_unknown_response_not_empty_seats(self):
        u=UZ();u.get=lambda *a,**k:{'unexpected':[]}
        with self.assertRaises(ProviderError):u.find(1,2,'2026-10-24','027К','20:25')
    def test_exact_departure_and_train(self):
        u=UZ();trip={'id':1,'train':{'number':'027К'},'depart_at':datetime(2026,10,24,20,25,tzinfo=KYIV).timestamp()}
        u.get=lambda *a,**k:{'direct':[trip]}
        self.assertEqual(u.find(1,2,'2026-10-24','027K','20:25'),trip)
        with self.assertRaises(ProviderError):u.find(1,2,'2026-10-24','027К','21:25')
    def test_wagons_endpoint_and_class(self):
        u=UZ();paths=[]
        def get(path):paths.append(path);return {'wagons':[wagon([1,3])]}
        u.get=get
        results,_=u.options({'id':17,'train':{'number':'027К','wagon_classes':[{'id':'К','name':'Купе'}]}},True)
        self.assertTrue(results);self.assertIn('/wagons-by-class/',paths[0])
    def test_missing_auth_stops_before_network(self):
        u=UZ();u.token=''
        with self.assertRaises(ProviderError) as e:u.get('/stations')
        self.assertTrue(e.exception.auth)
if __name__=='__main__':unittest.main()
