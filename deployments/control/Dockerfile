# control Dockerfile

# Builds a Flask-socketio based control server for the SPRAYai system.

# Callum Morrison, 2021

FROM python:3.7

# Install requirements
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Copy program files
COPY app/ /app/
COPY run.py /run.py

EXPOSE 5040

ENV REDIS_HOST=redis

CMD ["python", "/run.py"]