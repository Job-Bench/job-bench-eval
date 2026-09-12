# Appended to the shared eval/calc-runtime/Dockerfile when a task is rendered.
RUN /usr/bin/python3 -m venv /opt/jobbench-venv
ENV PATH="/opt/jobbench-venv/bin:${PATH}"

COPY requirements.txt /tmp/jobbench-verifier-requirements.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/jobbench-verifier-requirements.txt \
    && rm /tmp/jobbench-verifier-requirements.txt

COPY . /tests/
RUN chmod +x /tests/test.sh
WORKDIR /tests
