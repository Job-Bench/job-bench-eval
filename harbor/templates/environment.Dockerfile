FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash curl file git jq tmux unzip zip \
        sqlite3 poppler-utils tesseract-ocr libreoffice pandoc \
        gdal-bin libgl1 libglib2.0-0 \
        fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/jobbench-requirements.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/jobbench-requirements.txt \
    && rm /tmp/jobbench-requirements.txt

RUN mkdir -p /workspace/task_folder /workspace/output \
    && chmod 0777 /workspace/output
COPY task_folder/ /workspace/task_folder/

WORKDIR /workspace
