FROM python:3.12-slim

# Install GDAL system libraries required by geopandas/pyogrio/shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev \
    gdal-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching — only rebuilds if requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/
COPY schemas/ ./schemas/

# Copy the included GeoPackage data files
COPY data/ ./data/

# Expose the default uvicorn port
EXPOSE 8000

# Run the service
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
