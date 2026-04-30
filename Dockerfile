FROM python:3.11-slim

# Accept MS fonts EULA non-interactively
RUN echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ffmpeg \
    fontconfig \
    wget \
    ttf-mscorefonts-installer \
    fonts-crosextra-carlito \
    && rm -rf /var/lib/apt/lists/*

# Montserrat (Google Fonts — open source)
RUN mkdir -p /usr/share/fonts/truetype/montserrat \
    && wget -q "https://github.com/google/fonts/raw/main/ofl/montserrat/static/Montserrat-Regular.ttf" \
       -O /usr/share/fonts/truetype/montserrat/Montserrat-Regular.ttf \
    && wget -q "https://github.com/google/fonts/raw/main/ofl/montserrat/static/Montserrat-Bold.ttf" \
       -O /usr/share/fonts/truetype/montserrat/Montserrat-Bold.ttf \
    && fc-cache -f

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD uvicorn app:app --host 0.0.0.0 --port $PORT
