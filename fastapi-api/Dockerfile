FROM python:3.11-slim

WORKDIR /app

COPY fastapi-api/requirements.txt .
RUN pip install --no-cache-dir \
	--trusted-host pypi.org \
	--trusted-host files.pythonhosted.org \
	-r requirements.txt

COPY fastapi-api/*.py ./
COPY fastapi-api/static ./static
COPY Scada ./Scada

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
