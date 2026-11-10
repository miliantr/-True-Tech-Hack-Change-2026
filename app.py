"""Local prototype server. Run python app.py or docker compose up --build."""
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from config import Config, env
from consultant import Consultant, UNAVAILABLE
from retrieval import locate_kb
from storage import Store

ROOT = Path(__file__).resolve().parent


def load_dotenv(path):
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=",1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def create_server(host="127.0.0.1", port=8000, engine=None, store=None, review_token=None):
    config = Config.from_env()
    engine = engine or Consultant(locate_kb(env("DATA_DIR",default=str(ROOT/"Данные"))), config)
    runtime = Path(env("RUNTIME_DIR",default=str(ROOT/"runtime")))
    runtime.mkdir(parents=True,exist_ok=True)
    store = store or Store(runtime/"energytool.sqlite3")
    if review_token is None:
        review_token = env("REVIEW_TOKEN")
        if not review_token:
            token_file = runtime/"reviewer-token.txt"
            if not token_file.exists():
                token_file.write_text(secrets.token_urlsafe(32),encoding="utf-8")
                try:
                    token_file.chmod(0o600)
                except OSError:
                    pass
            review_token = token_file.read_text(encoding="utf-8").strip()
    slots = threading.BoundedSemaphore(config.workers)
    allowed_origin = env("PUBLIC_ORIGIN")

    class Handler(BaseHTTPRequestHandler):
        server_version = "EnergyTool/1.0"

        def log_message(self, format, *args):
            # Request bodies, session capabilities and API secrets are never logged.
            pass

        def send(self, code, payload, content_type="application/json; charset=utf-8"):
            data = json.dumps(payload,ensure_ascii=False).encode("utf-8") if content_type.startswith("application/json") else payload
            self.send_response(code)
            self.send_header("Content-Type",content_type)
            self.send_header("Content-Length",str(len(data)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Referrer-Policy","no-referrer")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):
                pass

        def origin_ok(self):
            host_header = self.headers.get("Host", "")
            permitted = {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}
            if allowed_origin:
                permitted.add(urlsplit(allowed_origin).netloc)
            if host_header not in permitted:
                return False
            origin = self.headers.get("Origin")
            return not origin or origin in {f"http://{host_header}", allowed_origin}

        def body(self):
            if self.headers.get("Content-Type","").split(";")[0] != "application/json":
                raise ValueError("Ожидается application/json")
            length = int(self.headers.get("Content-Length","0"))
            if not 0 < length <= 65536:
                raise ValueError("Некорректный размер запроса")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError("Ожидается JSON-объект")
            return data

        def reviewer(self):
            return secrets.compare_digest(self.headers.get("X-Reviewer-Token", "").encode(), review_token.encode())

        def do_GET(self):
            if not self.origin_ok():
                return self.send(403,{"error":"Origin rejected"})
            path = urlsplit(self.path).path
            assets = {"/":"index.html","/app.js":"app.js","/style.css":"style.css"}
            if path in assets:
                file = ROOT/"static"/assets[path]
                kind = {".html":"text/html", ".js":"text/javascript", ".css":"text/css"}[file.suffix]
                return self.send(200,file.read_bytes(),kind+"; charset=utf-8")
            if path == "/api/status":
                return self.send(200,{"mode":"live" if engine.config.live else "preview", "chunks":len(engine.index.chunks)})
            if path.startswith("/api/review"):
                if not self.reviewer():
                    return self.send(401,{"error":"Нужен ключ режима проверки"})
                if path == "/api/review/sessions":
                    return self.send(200,{"sessions":store.sessions()})
                parts = path.split("/")
                if len(parts)==5 and parts[3]=="sessions":
                    return self.send(200,{"messages":store.messages(parts[4]),"lead":store.lead(parts[4])})
            return self.send(404,{"error":"Не найдено"})

        def do_POST(self):
            if not self.origin_ok():
                return self.send(403,{"error":"Origin rejected"})
            self.connection.settimeout(120)
            try:
                data = self.body()
                path = urlsplit(self.path).path
                if path == "/api/sessions":
                    return self.send(201,store.create_session())
                sid = data.get("session_id","")
                if not isinstance(sid,str) or not store.authorized(sid,self.headers.get("X-Session-Token","")):
                    return self.send(403,{"error":"Диалог недоступен"})
                if path == "/api/close":
                    store.close(sid)
                    return self.send(200,{"closed":True})
                if path == "/api/leads":
                    return self.send(200,{"lead":store.create_lead(sid,data)})
                if path == "/api/chat":
                    text = data.get("message")
                    if not isinstance(text,str) or not 1 <= len(text.strip()) <= 8000:
                        raise ValueError("Сообщение должно содержать от 1 до 8000 символов")
                    if not slots.acquire(blocking=False):
                        return self.send(429,{"error":"Сервис занят. Повторите через несколько секунд."})
                    try:
                        store.begin_turn(sid,text.strip())
                        try:
                            messages = [{"role":m["role"],"content":m["content"]} for m in store.messages(sid)]
                            result = engine.answer(messages,preview=True)
                        except Exception:
                            result = {"answer":UNAVAILABLE,"mode":"unavailable","debug":{"elapsed_ms":0,"sources":[],"warnings":["Internal request failure"]}}
                        store.end_turn(sid,result)
                        # Source text is restricted to the authenticated reviewer view.
                        return self.send(200,{"answer":result["answer"],"mode":result["mode"],"elapsed_ms":result["debug"]["elapsed_ms"]})
                    finally:
                        slots.release()
                return self.send(404,{"error":"Не найдено"})
            except (ValueError,TypeError,UnicodeError):
                return self.send(400,{"error":"Некорректные данные. Проверьте поля или состояние диалога."})

    server = ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    load_dotenv(ROOT/".env")
    server = create_server(env("HOST",default="127.0.0.1"),int(env("PORT",default="8000")))
    print(f"EnergyTool: http://localhost:{server.server_port}", flush=True)
    if not env("REVIEW_TOKEN"):
        runtime = Path(env("RUNTIME_DIR", default=str(ROOT / "runtime")))
        print(f"Reviewer key file: {runtime / 'reviewer-token.txt'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
