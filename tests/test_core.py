import csv
import io
import json
import tempfile
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from api_client import APIClient, ServiceError
from config import Config
from consultant import Consultant, parse_dialogue
from package_submission import FILES, package
from retrieval import Index, chunk_documents, load_documents, tokens
from solution import read_questions, write_submission
from storage import Store

ROOT = Path(__file__).resolve().parents[1]


class RetrievalTests(unittest.TestCase):
    def test_metering_intent_without_product_name(self):
        index = Index(load_documents(ROOT/'Данные/knowledge-base.zip'))
        question = 'У нас три склада. Хотим видеть потребление каждого склада.'
        first, _ = index.lexical(question)[0]
        self.assertTrue(index.chunks[first].source.endswith('22-askue.md'))

    def test_whole_documents_and_table(self):
        docs = [("test.md", "# Product\n" + "Intro. "*100 + "\n## Deep fact\nneedle 750 кВт\n\n A     B\n ---   ---\n 1     2\n 3     4")]
        index = Index(docs)
        self.assertIn("needle", index.chunks[index.lexical("needle")[0][0]].text)
        self.assertTrue(any("1     2\n 3     4" in c.text for c in index.chunks))

    def test_short_terms_and_units(self):
        self.assertIn("снт",tokens("СНТ на ВН, 750 кВт и 1000 кВт·ч"))
        self.assertIn("вн",tokens("СНТ на ВН"))

    def test_escaped_heading(self):
        chunks=chunk_documents([("test.md","# A\nHello \\## Needed\nDetails")])
        self.assertTrue(any("Needed" in c.heading for c in chunks))

    def test_zip_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"bad.zip"
            with zipfile.ZipFile(path,"w") as z:z.writestr("../evil.md","bad")
            with self.assertRaises(ValueError):load_documents(path)

    def test_archive_matches_directory(self):
        archive=dict(load_documents(ROOT/"Данные/knowledge-base.zip"))
        self.assertEqual(archive,dict(load_documents(ROOT/"Данные")))
        self.assertEqual(len(archive),19)

    def test_hybrid_failure_keeps_lexical(self):
        index=Index([("a.md","# A\nэлектроэнергия")])
        client=APIClient(Config(retrieval="hybrid"))
        index.prepare(client)
        chunks,warnings=index.search("электроэнергия",client)
        self.assertEqual(len(chunks),1)
        self.assertTrue(warnings)


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.engine=Consultant(ROOT/"Данные/knowledge-base.zip",Config())

    def test_dialogue_last_user(self):
        messages=parse_dialogue("Пользователь: Завод\nАссистент: Мощность?\nПользователь: 750 кВт")
        self.assertEqual([m['role'] for m in messages],["user","assistant","user"])
        self.assertEqual(messages[-1]["content"],"750 кВт")

    def test_payback_paraphrases_retrieve_both_qualified_ranges(self):
        for question in ("Через сколько лет окупится СЭС?", "Каковы сроки возврата инвестиций в СЭС?"):
            with self.subTest(question=question):
                chunks, _ = self.engine.retrieve(parse_dialogue(question))
                evidence = " ".join(" ".join(chunk.text.split()) for chunk in chunks if chunk.source.endswith('23-solar.md'))
                self.assertIn("от 7 до 9", evidence)
                self.assertIn("от 4 до 6", evidence)

    def test_latest_topic_gets_evidence_without_losing_previous_topic(self):
        messages = parse_dialogue(
            "Пользователь: Давно обсуждаем выход на оптовый рынок, расчёт экономии, 750 кВт, 20 МВт, обслуживание.\n"
            "Ассистент: Какой объём потребления?\n"
            "Пользователь: А по фото сможем снять показания водомера?"
        )
        chunks, _ = self.engine.retrieve(messages)
        self.assertTrue(chunks[0].source.endswith('25-meter-reading.md'))
        self.assertTrue(any(chunk.source.endswith('21-orem.md') for chunk in chunks))

    def test_preview_explicit(self):
        answer=self.engine.answer([{"role":"user","content":"Солнечная станция на крыше"}],preview=True)
        self.assertEqual(answer["mode"],"preview")
        self.assertIn("Предпросмотр без LLM",answer["answer"])
        self.assertTrue(answer["debug"]["sources"])

    def test_no_silent_preview_submission(self):
        answer=self.engine.answer([{"role":"user","content":"Солнечная станция"}])
        self.assertEqual(answer["mode"],"unavailable")

    def test_live_history_and_evidence(self):
        engine=Consultant(ROOT/"Данные/knowledge-base.zip",Config(base_url="http://localhost",api_key="test",model="test"))
        with patch.object(engine.client,"generate",return_value="Да, с оговорками.") as generate:
            result=engine.answer(parse_dialogue("Пользователь: Панели на крышу\nАссистент: Регион?\nПользователь: Москва"))
        messages=generate.call_args.args[0]
        self.assertIn("20-products/23-solar.md",messages[0]["content"])
        self.assertEqual([m['role'] for m in messages].count('system'), 1)
        self.assertEqual(messages[-1]["content"],"Москва")
        self.assertEqual(result["mode"],"live")

    def test_oversized_generation_rejected(self):
        engine=Consultant(ROOT/"Данные/knowledge-base.zip",Config(base_url="http://localhost",api_key="test",model="test"))
        with patch.object(engine.client,"generate",return_value="x"*10001):
            self.assertEqual(engine.answer([{"role":"user","content":"Солнечная станция"}])["mode"],"unavailable")


class APITests(unittest.TestCase):
    def setUp(self):self.client=APIClient(Config(base_url="http://localhost",api_key="test",model="test"))

    def test_embedding_order(self):
        with patch.object(self.client,"post",return_value={"data":[{"index":1,"embedding":[0,2]},{"index":0,"embedding":[3,0]}]}):
            self.assertEqual(self.client.embeddings(["a","b"]),[[1,0],[0,1]])

    def test_duplicate_embedding_index(self):
        with patch.object(self.client,"post",return_value={"data":[{"index":0,"embedding":[1]},{"index":0,"embedding":[2]}]}):
            with self.assertRaises(ServiceError):self.client.embeddings(["a","b"])

    def test_deadline(self):
        with self.assertRaises(ServiceError):self.client.post('/chat/completions',{},time.monotonic()-1)

    def test_empty_generation(self):
        with patch.object(self.client,"post",return_value={"choices":[{"message":{"content":""}}]}):
            with self.assertRaises(ServiceError):self.client.generate([])

    def test_truncation(self):
        with patch.object(self.client,"post",return_value={"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}):
            with self.assertRaises(ServiceError):self.client.generate([])

    def test_no_retry_401(self):
        import urllib.error
        with patch('urllib.request.urlopen',side_effect=urllib.error.HTTPError('http://localhost',401,'',{},None)) as call:
            with self.assertRaises(ServiceError):self.client.post('/chat/completions',{})
            self.assertEqual(call.call_count,1)

    def test_transient_retry(self):
        import urllib.error
        response=io.BytesIO(b'{"ok":true}')
        with patch('urllib.request.urlopen',side_effect=[urllib.error.HTTPError('http://localhost',429,'',{},None),response]) as call, patch('time.sleep'):
            self.assertTrue(self.client.post('/chat/completions',{})['ok'])
            self.assertEqual(call.call_count,2)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'test.db');self.session=self.store.create_session()

    def test_isolation_and_reset(self):
        self.store.begin_turn(self.session['id'],'Москва, завод')
        self.store.end_turn(self.session['id'],{'answer':'Понял'})
        self.store.close(self.session['id'])
        new=self.store.create_session()
        self.assertEqual(self.store.messages(new['id']),[])
        self.assertEqual(len(self.store.messages(self.session['id'])),2)
        self.assertFalse(self.store.authorized(new['id'],self.session['token']))
        with self.assertRaises(ValueError):self.store.begin_turn(self.session['id'],'again')

    def test_parallel_turn_rejected(self):
        self.store.begin_turn(self.session['id'],'first')
        with self.assertRaises(ValueError):self.store.begin_turn(self.session['id'],'second')

    def test_lead_validated_idempotent(self):
        sid=self.session['id'];self.store.begin_turn(sid,'У нас три склада')
        self.store.end_turn(sid,{'answer':'Уточните приборы'})
        fields={'name':'Тест Тестов','phone':'+7 000 000-00-00','company':'Тест','test_data_confirmed':True}
        lead=self.store.create_lead(sid,fields)
        self.assertEqual(lead['id'],self.store.create_lead(sid,fields)['id'])
        self.assertEqual(json.loads(lead['summary'])['customer_request'],'У нас три склада')
        with self.assertRaises(ValueError):self.store.create_lead(sid,{**fields,'phone':'bad'})
        with self.assertRaises(ValueError):self.store.create_lead(sid,{**fields,'test_data_confirmed':False})

    def test_persists_restart(self):
        self.store.begin_turn(self.session['id'],'hello');self.store.end_turn(self.session['id'],{'answer':'yes'})
        another=Store(self.store.path)
        self.assertEqual(len(another.messages(self.session['id'])),2)


class PackagingTests(unittest.TestCase):
    def test_exact_ids_csv_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'in.csv';p.write_text('id,question\n" 001 ","Привет, мир"\n',encoding='utf-8')
            rows=read_questions(p);out=Path(tmp)/'out.csv';write_submission(out,rows,['Ответ "да",\nтест'])
            with out.open(encoding='utf-8',newline='') as f:result=list(csv.DictReader(f))
            self.assertEqual(result,[{'id':' 001 ','answer':'Ответ "да", тест'}])

    def test_duplicate_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'in.csv';p.write_text('id,question\n1,a\n1,b\n',encoding='utf-8')
            with self.assertRaises(ValueError):read_questions(p)

    def test_archive_only_allowed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(package(Path(tmp)/'submission.zip')) as z:
                self.assertEqual(set(z.namelist()),set(FILES))


if __name__=='__main__':unittest.main()
