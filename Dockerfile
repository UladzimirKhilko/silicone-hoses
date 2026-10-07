FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DJANGO_DEBUG=0 MEDIA_ROOT=/data/media

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN DJANGO_SECRET_KEY=build python manage.py collectstatic --noinput \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data/media /data/source && chown -R app /data \
    && chmod +x deploy/*.sh
USER app

EXPOSE 8000
ENTRYPOINT ["deploy/entrypoint.sh"]
CMD ["gunicorn", "hoses.wsgi", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
