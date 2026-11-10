FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000 DATA_DIR=/data RUNTIME_DIR=/app/runtime
RUN useradd --create-home --uid 10001 app && mkdir /app/runtime && chown app:app /app/runtime
COPY app.py config.py consultant.py retrieval.py api_client.py storage.py ./
COPY static ./static
COPY Данные/knowledge-base.zip /data/knowledge-base.zip
USER app
EXPOSE 8000
CMD ["python", "app.py"]
