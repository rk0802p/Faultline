FROM python:3.11-slim

WORKDIR /app

COPY . /app

EXPOSE 8000 8766 8767

ENV PYTHONUNBUFFERED=1
ENV FAULTLINE_DATA_DIR=/app/data

HEALTHCHECK --interval=5s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1

CMD ["python", "-m", "faultline"]
