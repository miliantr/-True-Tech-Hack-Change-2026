"""Heading-aware retrieval over the supplied, private knowledge archive."""
import math
import re
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from api_client import ServiceError

WORD = re.compile(r"[а-яёa-z0-9]+", re.I)
MONITORING = re.compile(r"(?:видеть|сравнивать|отслеживать|контролировать)\s+(?:\w+\s+){0,3}(?:потреблен|расход)", re.I)
PAYBACK = re.compile(r"\b(?:окуп\w*|возврат\w*\s+инвестиц\w*)", re.I)
STOP = set("а и в во на по с со к у о об от до из для что как это ли мы вы я он она они нас вас мне можно будет есть или не но то за при нам ваш наш the a to of".split())
ALIASES = {
    "сим": "сим sim оператор", "симки": "сим sim оператор", "симками": "сим sim оператор",
    "sim": "сим sim оператор", "фасад": "фасад вертикаль поверхност",
    "фасаде": "фасад вертикаль поверхност", "крыша": "кровл крыш",
    "крыше": "кровл крыш", "фото": "фотограф распознаван",
    "снимок": "фотограф распознаван", "опт": "оптов рынок",
    "орэм": "оптов рынок", "воровство": "хищен потер",
    "свет": "электроэнерг", "симкарты": "сим оператор",
    "проверите": "проверк аудит", "проверить": "проверк аудит", "сэс": "солнечная станция",
}

def topic_sources(query):
    query = query.lower().replace("ё", "е")
    topics = {
        "23-solar.md": r"солнеч|панел|кровл|кры[шс]|фасад|\bсэс\b",
        "25-meter-reading.md": r"фото|распозна|снимк",
        "24-losses.md": r"потер|хищен|воров",
        "21-orem.md": r"оптов|орэм",
        "22-askue.md": r"счетчик|прибор|показан|учет|магазин|арендатор|цех|симк|сим-карт",
    }
    sources = {name for name, pattern in topics.items() if re.search(pattern, query)}
    if MONITORING.search(query):
        sources.add("22-askue.md")
    return sources


def tokens(text):
    result = []
    for word in WORD.findall(text.lower().replace("ё", "е")):
        if word in STOP:
            continue
        result.append(word)
        # A prefix feature handles Russian inflections without an extra NLP model.
        if len(word) > 5 and re.match("[а-я]", word):
            result.append(word[:5])
    return result


def expanded(text):
    aliases = [ALIASES.get(word, "") for word in WORD.findall(text.lower().replace("ё", "е"))]
    if MONITORING.search(text):
        aliases.append("платформа учёт приборы объекты")
    if PAYBACK.search(text):
        aliases.append("окупаемость экономический эффект")
    return " ".join([text, *aliases])


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str
    heading: str
    text: str


def load_documents(path):
    path = Path(path)
    docs = []
    if path.is_dir():
        for p in sorted(path.rglob("*.md")):
            if p.relative_to(path).parts[0] in ("00-reference", "10-energy", "20-products"):
                docs.append((p.relative_to(path).as_posix(), p.read_text(encoding="utf-8-sig")))
    else:
        with zipfile.ZipFile(path) as archive:
            total = 0
            for info in sorted(archive.infolist(), key=lambda x: x.filename):
                name = PurePosixPath(info.filename)
                if info.is_dir() or name.suffix.lower() not in (".md", ".txt") or "__MACOSX" in name.parts:
                    continue
                if name.is_absolute() or ".." in name.parts or "\\" in info.filename:
                    raise ValueError("Unsafe knowledge archive path")
                total += info.file_size
                if info.file_size > 2_000_000 or total > 20_000_000:
                    raise ValueError("Knowledge archive exceeds limits")
                docs.append((name.as_posix(), archive.read(info).decode("utf-8-sig")))
    if not docs:
        raise ValueError("Knowledge base contains no supported documents")
    return docs


def chunk_documents(documents, limit=2600):
    chunks = []
    for source, raw in documents:
        text = re.sub(r"\\(#{2,3}) ", r"\n\n\1 ", raw).replace("\r\n", "\n")
        headings, body = [], []

        def flush():
            content = "\n".join(body).strip()
            if not content:
                return
            # Blank-line groups preserve tables and their row continuations.
            groups = re.split(r"\n\s*\n", content)
            current = ""
            for group in groups:
                if current and len(current) + len(group) > limit:
                    chunks.append(Chunk(f"c{len(chunks):04}", source, " / ".join(headings), current.strip()))
                    current = ""
                current += group + "\n\n"
            if current.strip():
                chunks.append(Chunk(f"c{len(chunks):04}", source, " / ".join(headings), current.strip()))

        for line in text.splitlines():
            match = re.match(r"^(#{1,3})\s+(.+)$", line)
            if match:
                flush()
                body = []
                depth = len(match[1])
                headings = headings[:depth - 1] + [match[2]]
            else:
                body.append(line)
        flush()
    return chunks


class Index:
    def __init__(self, documents):
        self.chunks = chunk_documents(documents)
        self.counts = [Counter(tokens(c.heading + " " + c.heading + " " + c.text)) for c in self.chunks]
        self.lengths = [sum(c.values()) for c in self.counts]
        self.average = sum(self.lengths) / max(len(self.lengths), 1)
        frequencies = Counter(t for c in self.counts for t in c)
        self.idf = {t: math.log(1 + (len(self.chunks)-n+0.5)/(n+0.5)) for t, n in frequencies.items()}
        self.vectors = None
        self.index_warning = None

    def prepare(self, client, deadline=None):
        if client.config.retrieval != "hybrid":
            return
        try:
            vectors = []
            for start in range(0, len(self.chunks), 24):
                batch = self.chunks[start:start+24]
                vectors.extend(client.embeddings([c.heading + "\n" + c.text for c in batch], deadline))
            if len({len(v) for v in vectors}) != 1:
                raise ServiceError("Embedding dimension changed")
            self.vectors = vectors
        except ServiceError as exc:
            self.index_warning = str(exc)

    def lexical(self, query):
        terms = sorted(set(tokens(expanded(query))))
        topics = topic_sources(query)
        suitability = re.search(r"подой[дт]|подходит|имеет смысл", query.lower())
        load_profile = re.search(r"вечер|дневн|дн[её]м|ночн", query.lower())
        scores = []
        for i, counts in enumerate(self.counts):
            score = sum(self.idf.get(t, 0) * counts[t] * 2.5 /
                        (counts[t] + 1.5 * (0.25 + 0.75*self.lengths[i]/max(1,self.average)))
                        for t in terms if counts[t])
            if score > 0:
                if Path(self.chunks[i].source).name in topics:
                    score *= 1.8
                    heading = self.chunks[i].heading
                    if suitability and "Кому подходит" in heading:
                        score *= 1.4
                    if load_profile and heading.endswith("Лучший профиль клиента"):
                        score *= 2
                scores.append((i, score))
        return sorted(scores, key=lambda x: (-x[1], x[0]))

    def search(self, query, client, deadline=None, focus=""):
        config = client.config
        ranked = self.lexical(query)
        focus_topics = topic_sources(focus)
        if focus_topics and topic_sources(query) - focus_topics:
            # Give the latest topic room without discarding evidence from earlier turns.
            focused = [(i, score) for i, score in self.lexical(focus)
                       if Path(self.chunks[i].source).name in focus_topics][:2]
            focused_ids = {i for i, _ in focused}
            ranked = focused + [(i, score) for i, score in ranked if i not in focused_ids]
        warnings = [self.index_warning] if self.index_warning else []
        if self.vectors:
            try:
                vector = client.embeddings([query], deadline)[0]
                if len(vector) != len(self.vectors[0]):
                    raise ServiceError("Embedding dimension mismatch")
                dense = sorted(range(len(self.chunks)), key=lambda i: -sum(a*b for a,b in zip(vector,self.vectors[i])))
                fused = Counter()
                for order in ([i for i,_ in ranked[:40]], dense[:40]):
                    for rank, i in enumerate(order):
                        fused[i] += 1/(60+rank+1)
                ranked = fused.most_common()
            except ServiceError as exc:
                warnings.append(str(exc))
        candidates = ranked[:config.top_k if config.rerank else config.top_n]
        if config.rerank and candidates:
            try:
                result = client.post("/rerank", {
                    "model": config.reranker_model, "query": query,
                    "documents": [self.chunks[i].heading + "\n" + self.chunks[i].text for i,_ in candidates],
                    "top_n": min(config.top_n, len(candidates)),
                }, deadline)
                entries = result.get("results", result.get("data", []))
                positions = [int(e["index"]) for e in entries]
                if not positions or len(set(positions)) != len(positions) or any(not 0 <= p < len(candidates) for p in positions):
                    raise ValueError()
                ordered = [candidates[p] for p in positions]
                candidates = ordered + [c for p,c in enumerate(candidates) if p not in positions]
            except (ServiceError, KeyError, TypeError, ValueError):
                warnings.append("Reranker unavailable; retained retrieval order")
        chosen = [self.chunks[i] for i,_ in candidates[:config.top_n]]
        # Include product restrictions as evidence, rather than relying on model memory.
        product_sources = {c.source for c in chosen[:3] if c.source.startswith("20-products/")}
        supplements = [c for c in self.chunks if c.source in product_sources and
                       ("Чего утверждать нельзя" in c.heading or "Что мы не делаем" in c.heading)]
        reference_headings = {
            "21-orem.md": "Для анализа затрат и выхода на оптовый рынок",
            "22-askue.md": "Для платформы учёта",
            "23-solar.md": "Для солнечной станции",
            "24-losses.md": "Для поиска коммерческих потерь",
            "25-meter-reading.md": "Для распознавания показаний",
        }
        needed = {reference_headings.get(Path(s).name) for s in product_sources}
        supplements += [c for c in self.chunks if c.source.endswith("03-calculation-and-requests.md")
                        and any(h and c.heading.endswith(h) for h in needed)]
        if chosen:
            supplements += [c for c in self.chunks if c.source.endswith("02-product-scope-and-limits.md") and c.heading.endswith("Общие границы")]
        context, seen, size = [], set(), 0
        for chunk in chosen + supplements:
            key = chunk.text
            if key not in seen and size + len(chunk.text) < 22000:
                context.append(chunk)
                seen.add(key)
                size += len(chunk.text)
        return context, warnings


def locate_kb(data_dir):
    root = Path(data_dir)
    for name in ("knowledge-base.zip", "knowledge_base.zip", "kb.zip"):
        if (root / name).is_file():
            return root / name
    if (root / "00-reference").is_dir():
        return root
    raise FileNotFoundError("Knowledge base not found in DATA_DIR")
