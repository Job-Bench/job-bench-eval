FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/jobbench-verifier-requirements.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/jobbench-verifier-requirements.txt \
    && rm /tmp/jobbench-verifier-requirements.txt

COPY . /tests/
RUN chmod +x /tests/test.sh
WORKDIR /tests
