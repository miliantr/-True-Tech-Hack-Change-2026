import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from app import create_server
from config import Config
from consultant import Consultant
from storage import Store

ROOT=Path(__file__).resolve().parents[1]


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.engine=Consultant(ROOT/'Данные/knowledge-base.zip',Config())
        with patch.dict('os.environ',{'RUNTIME_DIR':cls.temp.name}):
            cls.server=create_server(port=0,engine=cls.engine,store=Store(Path(cls.temp.name)/'db.sqlite3'),review_token='test-review')
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()

    def call(self,path,data=None,headers=None):
        req=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type':'application/json',**(headers or {})})
        try:
            with urllib.request.urlopen(req) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as e:return e.code,json.load(e)

    def test_full_journey_and_review(self):
        status,session=self.call('/api/sessions',{});self.assertEqual(status,201)
        headers={'X-Session-Token':session['token']}
        status,result=self.call('/api/chat',{'session_id':session['id'],'message':'Панели на фасад'},headers)
        self.assertEqual(status,200);self.assertEqual(result['mode'],'preview');self.assertNotIn('debug',result)
        fields={'session_id':session['id'],'name':'Тест Тестов','phone':'+70000000000','company':'Тест','test_data_confirmed':True}
        status,result=self.call('/api/leads',fields,headers);self.assertEqual(status,200)
        self.assertEqual(self.call('/api/review/sessions')[0],401)
        status,detail=self.call('/api/review/sessions/'+session['id'],headers={'X-Reviewer-Token':'test-review'})
        self.assertEqual(status,200);self.assertEqual(len(detail['messages']),2);self.assertTrue(detail['lead'])
        self.assertTrue(detail['messages'][-1]['debug']['debug']['sources'])
        self.assertEqual(self.call('/api/close',{'session_id':session['id']},headers)[0],200)
        self.assertEqual(self.call('/api/chat',{'session_id':session['id'],'message':'again'},headers)[0],400)

    def test_cross_session_denied(self):
        _,a=self.call('/api/sessions',{});_,b=self.call('/api/sessions',{})
        self.assertEqual(self.call('/api/chat',{'session_id':b['id'],'message':'test'},{'X-Session-Token':a['token']})[0],403)

    def test_lead_validation_correction_and_decline(self):
        _, session = self.call('/api/sessions', {})
        sid = session['id']
        headers = {'X-Session-Token': session['token']}
        review = {'X-Reviewer-Token': 'test-review'}
        self.assertEqual(self.call('/api/chat', {'session_id': sid, 'message': 'Нужен учёт по складам'}, headers)[0], 200)
        fields = {'session_id': sid, 'name': 'Тест Тестов', 'phone': '+70000000000', 'test_data_confirmed': True}
        self.assertEqual(self.call('/api/leads', fields, headers)[0], 400)
        fields['company'] = 'Тестовый склад'
        self.assertEqual(self.call('/api/leads', {**fields, 'phone': '123'}, headers)[0], 400)
        self.assertEqual(self.call('/api/chat', {'session_id': sid, 'message': 'Пока продолжим без заявки'}, headers)[0], 200)
        _, detail = self.call('/api/review/sessions/'+sid, headers=review)
        self.assertIsNone(detail['lead'])
        self.assertEqual(len(detail['messages']), 4)
        status, saved = self.call('/api/leads', fields, headers)
        self.assertEqual(status, 200)
        self.assertEqual(saved['lead']['phone'], fields['phone'])
        _, repeated = self.call('/api/leads', fields, headers)
        self.assertEqual(repeated['lead']['id'], saved['lead']['id'])

    def test_foreign_origin_denied(self):
        self.assertEqual(self.call('/api/sessions',{}, {'Origin':'https://evil.example'})[0],403)

    def test_xss_stored_as_text(self):
        _,session=self.call('/api/sessions',{})
        status,_=self.call('/api/chat',{'session_id':session['id'],'message':'<img src=x onerror=alert(1)>'},{'X-Session-Token':session['token']})
        self.assertEqual(status,200)
        self.assertNotIn('innerHTML', (ROOT/'static/app.js').read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
