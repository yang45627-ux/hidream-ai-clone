FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py engine.py operations.py identity.py business.py connectors.py database.py finance.py storefront.py net.py runtime.py research.py director.py media.py growth.py integrations.py workflows.py limits.py model_connection.py ./
COPY web ./web
COPY tools ./tools
RUN useradd --create-home --uid 10001 appuser && mkdir /app/.data && chown -R appuser:appuser /app
USER appuser
ENV APP_HOST=0.0.0.0 PORT=3000 APP_DB=/app/.data/app.db
EXPOSE 3000
CMD ["python3", "server.py"]
