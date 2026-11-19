# syntax=docker/dockerfile:1
# Two images from one source tree (decision 0007):
#   safeops-testbed  - the system under test (gateway, orders, inventory, payment)
#   safeops-loadgen  - the workload generator (measurement infrastructure)
# They share only the base stage (Python, prometheus-client, app/common.py). Measurement
# code (window.py, loadgen.py) is never copied into the testbed stage, so changing it
# cannot alter the system-under-test image.

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY requirements-base.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements-base.txt
COPY app/__init__.py app/common.py app/

FROM base AS testbed
COPY requirements-testbed.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements-testbed.txt
COPY app/service.py app/
USER 10001
EXPOSE 8080
CMD ["python", "-m", "app.service"]

FROM base AS loadgen
COPY app/window.py app/loadgen.py app/
USER 10001
EXPOSE 8080
CMD ["python", "-m", "app.loadgen"]
