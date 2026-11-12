"""Run this with the bot's Python to find out why music won't play."""
import shutil
import subprocess
import sys
 
print("=== Music bot diagnostics ===")
print("Python:", sys.version.split()[0])
print("ffmpeg on PATH:", shutil.which("ffmpeg") or "NOT FOUND")
 
try:
    import nacl
    print("PyNaCl: OK", getattr(nacl, "__version__", ""))
except Exception as e:
    print("PyNaCl: PROBLEM ->", repr(e))
 
try:
    import discord
    print("discord.py:", discord.__version__)
    try:
        discord.opus.Encoder()
        print("Opus: OK")
    except Exception as e:
        print("Opus: PROBLEM ->", repr(e))
except Exception as e:
    print("discord.py: PROBLEM ->", repr(e))
 
def find_stream(search):
    import yt_dlp
    opts = {"format": "bestaudio/best", "quiet": True, "noplaylist": True, "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        results = ydl.extract_info(search, download=False)["entries"]
    with yt_dlp.YoutubeDL({"format": "bestaudio/best", "quiet": True, "noplaylist": True}) as ydl:
        for entry in results:
            try:
                info = ydl.extract_info(entry["url"], download=False)
                print("Found:", info.get("title"))
                return info["url"]
            except Exception as e:
                print("Skipped a result:", str(e)[:160])
    return None
 
 
def test_ffmpeg(url):
    cmd = ["ffmpeg", "-v", "error", "-reconnect", "1", "-reconnect_streamed", "1",
           "-reconnect_delay_max", "5", "-i", url, "-t", "3", "-vn", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        print("ffmpeg exit code:", r.returncode, "(0 means success)")
        if r.stderr.strip():
            print("ffmpeg said:", r.stderr.strip()[-800:])
    except Exception as e:
        print("ffmpeg: PROBLEM ->", repr(e))
 
 
try:
    import yt_dlp
    print("yt-dlp:", yt_dlp.version.__version__)
except Exception as e:
    print("yt-dlp: PROBLEM ->", repr(e))
 
print("Deno (used by yt-dlp for YouTube):", shutil.which("deno") or "NOT FOUND")
 
for name, search in (("YouTube", "ytsearch5:lofi hip hop"), ("SoundCloud", "scsearch5:lofi hip hop")):
    print(f"\n--- {name}: find a stream ---")
    try:
        url = find_stream(search)
    except Exception as e:
        url = None
        print(f"{name} search: PROBLEM ->", str(e)[:400])
    if url and shutil.which("ffmpeg"):
        print(f"--- {name}: let ffmpeg decode 3 seconds ---")
        test_ffmpeg(url)
    elif not url:
        print(f"No {name} stream found, so the ffmpeg test was skipped.")
 
print("\n=== Done. Send me everything above. ===")
 
