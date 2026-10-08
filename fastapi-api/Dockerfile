FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir \
	--trusted-host pypi.org \
	--trusted-host files.pythonhosted.org \
	-r requirements.txt

COPY main.py auth.py dictionary.py downtime.py manual.py comments.py activity.py history.py period.py reports.py colwidths.py backups.py ./
COPY static ./static

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
