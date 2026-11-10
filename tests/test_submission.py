import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zipfile import ZipFile

from package_submission import package
from solution import read_questions

ROOT = Path(__file__).resolve().parents[1]


class SubmissionTest(unittest.TestCase):
    def test_lexical_submission(self):
        self.run_submission(hybrid=False)

    def test_hybrid_and_reranking_submission(self):
        self.run_submission(hybrid=True)

    def run_submission(self, hybrid):
        requests = []

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers['Authorization'], body))
                if self.path == '/v1/embeddings':
                    reply = {'data': [{'index': i, 'embedding': [1, len(text) % 13 + 1]} for i, text in enumerate(body['input'])]}
                elif self.path == '/v1/rerank':
                    reply = {'results': [{'index': i} for i in range(body['top_n'])]}
                else:
                    reply = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'Тестовый ответ прокси.'}}]}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(('127.0.0.1', 0), Proxy) as proxy:
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            try:
                folder = Path(tmp)
                with ZipFile(package(folder/'submission.zip')) as z:
                    z.extractall(folder)
                data_dir = folder/'data'
                data_dir.mkdir()
                shutil.copyfile(ROOT/'Данные/demo_questions.csv', data_dir/'questions.csv')
                shutil.copyfile(ROOT/'Данные/knowledge-base.zip', data_dir/'knowledge-base.zip')
                env = {**os.environ, 'LLM_BASE_URL': f'http://127.0.0.1:{proxy.server_port}/v1',
                       'LLM_API_KEY': 'fixture', 'LLM_MODEL': 'fixture-model', 'DATA_DIR': str(data_dir),
                       'RETRIEVAL_MODE': 'hybrid' if hybrid else 'lexical', 'USE_RERANKER': '1' if hybrid else '0',
                       'TOP_K': '16' if hybrid else '2', 'RERANKER_TOP_N': '2',
                       'ALLOW_PREVIEW': '0'}
                proc = subprocess.run([sys.executable, 'solution.py'], cwd=folder, env=env, capture_output=True, timeout=25)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors='replace'))
                with (folder/'submission.csv').open(encoding='utf-8', newline='') as f:
                    rows = list(csv.DictReader(f))
                self.assertEqual([row['id'] for row in rows], [row['id'] for row in read_questions(data_dir/'questions.csv')])
                self.assertTrue(all(row['answer'] == 'Тестовый ответ прокси.' for row in rows))
                self.assertTrue(all(auth == 'Bearer fixture' for _, auth, _ in requests))
                chats = [body for path, _, body in requests if path == '/v1/chat/completions']
                self.assertEqual(len(chats), 18)
                self.assertEqual(any(path == '/v1/embeddings' for path, _, _ in requests), hybrid)
                self.assertEqual(sum(path == '/v1/rerank' for path, _, _ in requests), 18 if hybrid else 0)
                history = next(body['messages'] for body in chats if 'вечерней нагрузкой' in body['messages'][-1]['content'])
                self.assertEqual([msg['role'] for msg in history], ['system', 'user', 'assistant', 'user', 'assistant', 'user'])
                self.assertIn(b'context=2' if hybrid else b'context=6', proc.stdout)
            finally:
                proxy.shutdown()
                thread.join()


if __name__ == '__main__':
    unittest.main()
