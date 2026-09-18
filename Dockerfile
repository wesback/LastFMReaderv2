FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml LICENSE ./
COPY lastfm_export ./lastfm_export

RUN pip install --no-cache-dir . \
    && useradd --create-home --shell /usr/sbin/nologin lastfm \
    && chown -R lastfm:lastfm /app

USER lastfm
ENTRYPOINT ["lastfm-export"]
