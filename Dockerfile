FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY . .
# compila os catálogos de tradução (.po -> .mo)
RUN pybabel compile -d translations
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]