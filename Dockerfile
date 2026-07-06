FROM python:3.12-slim

# Install GDAL system libraries required by geopandas/pyogrio/shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev \
    gdal-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching — only rebuilds if requirements change)
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/
COPY schemas/ ./schemas/
COPY main.py .
COPY prewarm.py .
COPY gunicorn.conf.py .

# Copy the included GeoPackage data files
COPY data/ ./data/

# Create a non-root user and transfer ownership of /app before switching.
# The data/ directory is often volume-mounted at runtime; ownership here only
# matters for the baked-in copy.  If you mount a host directory over /app/data,
# ensure the host directory is owned/readable by UID 1000 (lvfuser), e.g.:
#   sudo chown -R 1000:1000 ./data
RUN adduser --disabled-password --gecos "" lvfuser \
    && chown -R lvfuser:lvfuser /app
USER lvfuser

# Expose the default service port
EXPOSE 8000

# Pre-warm the GIS cache once, then launch gunicorn with the uvicorn worker
# class. Worker count is controlled by LVF_WORKERS (default 1 == single-process
# behavior, identical to the previous `python main.py` launch).
CMD ["sh", "-c", "python prewarm.py && exec gunicorn -c gunicorn.conf.py src.server:app"]
