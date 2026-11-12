FROM python:3.12-slim

# ffmpeg + opus for voice
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

# Deno: the JavaScript runtime yt-dlp uses to solve YouTube's challenges
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
# yt-dlp nightly fixes YouTube breakages fastest, so update it on every start
CMD ["sh", "-c", "pip install -q -U --pre 'yt-dlp[default]'; python bot.py"]
