FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir mysql-connector-python
COPY server.py import_csv.py index.html base.css theme.css theme-airgradient.css ./
EXPOSE 8080
CMD ["python3", "server.py"]
