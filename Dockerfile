FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY fetch_baseline.py /app/fetch_baseline.py
RUN python /app/fetch_baseline.py /opt/sqlglot
RUN python -m compileall -q --invalidation-mode checked-hash /opt/sqlglot/sqlglot /opt/sqlglot/tests
COPY selector.py prepare_map.py value_profile.py runtime_v2.py forkprobe.py coverage-map.json config-reads.json /app/
RUN python /app/prepare_map.py /app/coverage-map.json /app/map-index
CMD ["python", "/app/selector.py"]
