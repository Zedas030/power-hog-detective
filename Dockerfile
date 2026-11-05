# Stromfresser-Detektiv
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Berlin
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sfd/ sfd/

# test stage: `docker build --target test .` runs the unit and end-to-end tests
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ tests/
COPY config/config.example.yaml config/config.example.yaml
RUN python -m pytest -q -p no:cacheprovider tests/

FROM base AS app
RUN useradd --system --uid 10001 --home /data sfd && mkdir -p /data /config && chown sfd /data
USER sfd
VOLUME ["/data"]
EXPOSE 8101
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 CMD ["python", "-m", "sfd.healthcheck"]
ENTRYPOINT ["python", "-m", "sfd"]
CMD ["run"]
