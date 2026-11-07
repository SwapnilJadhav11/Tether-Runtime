# Demo target service image (not Tether). Built by deploy/compose/docker-compose.yml.
FROM python:3.12-slim
RUN pip install --no-cache-dir "fastapi==0.142.3" "uvicorn==0.54.0"
WORKDIR /srv
COPY app.py ./app.py
RUN useradd --system --uid 10002 --no-create-home demo
USER demo
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
