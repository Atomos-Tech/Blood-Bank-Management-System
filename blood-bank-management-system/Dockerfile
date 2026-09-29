FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
RUN python manage.py collectstatic --noinput
CMD ["gunicorn", "bbms.wsgi:application", "--bind", "0.0.0.0:8000"]
