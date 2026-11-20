FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY config ./config
ARG BUILD_CA_B64=""
RUN if [ -n "$BUILD_CA_B64" ]; then \
      printf '%s' "$BUILD_CA_B64" | base64 -d > /usr/local/share/ca-certificates/build-ca.crt && \
      update-ca-certificates; \
    fi && \
PIP_CERT=/etc/ssl/certs/ca-certificates.crt pip install --prefix=/install --no-cache-dir .

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /install/ /usr/local/
COPY --from=build /app/app ./app
COPY --from=build /app/config ./config
RUN useradd --system --uid 10001 agent
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
