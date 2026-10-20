FROM python:3.12-slim
WORKDIR /app
COPY server.py engine.py operations.py ./
COPY web ./web
RUN useradd --create-home --uid 10001 appuser && mkdir /app/.data && chown -R appuser:appuser /app
USER appuser
ENV APP_HOST=0.0.0.0 PORT=3000 APP_DB=/app/.data/app.db
EXPOSE 3000
CMD ["python3", "server.py"]
