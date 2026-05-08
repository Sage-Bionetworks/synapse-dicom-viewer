FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml .
COPY src/ src/

RUN uv pip install --system .

# Cloud Run sets PORT (default 8080); HOSTED=1 enables browser-PAT auth.
ENV PORT=8080
ENV HOSTED=1

CMD exec uvicorn synapse_dicom_viewer.proxy:app --host 0.0.0.0 --port ${PORT}
