import os
import json
import time
import pytz
import requests
import datetime
from flask import Flask, request

app = Flask(__name__)

# Cached values
cache = {
    "video_id": None,
    "start_time": None,
    "last_checked": 0
}

# Config
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
CACHE_DURATION = 300  # seconds

# Helper to check if channel is live and get stream start time
def get_cached_live_info():
    now = time.time()
    if cache["video_id"] and (now - cache["last_checked"] < CACHE_DURATION):
        print("[ℹ️] Using cached video ID.")
        return cache["video_id"], cache["start_time"]

    print("[🔍] Checking for live stream...")
    search_url = (
        f"https://www.googleapis.com/youtube/v3/search?part=snippet&channelId={CHANNEL_ID}"
        f"&eventType=live&type=video&key={YOUTUBE_API_KEY}"
    )
    response = requests.get(search_url)
    data = response.json()
    print("[📡] YouTube Search API Response:", json.dumps(data, indent=2))

    if "items" in data and len(data["items"]) > 0:
        video_id = data["items"][0]["id"]["videoId"]
        cache["video_id"] = video_id
        cache["last_checked"] = now
        print(f"[✅] Live video found: {video_id}")

        # Get stream start time
        video_url = (
            f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={video_id}"
            f"&key={YOUTUBE_API_KEY}"
        )
        details = requests.get(video_url).json()
        print("[🕒] Live Streaming Details Response:", json.dumps(details, indent=2))

        try:
            start_time = details["items"][0]["liveStreamingDetails"]["actualStartTime"]
            start_dt = datetime.datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            cache["start_time"] = start_dt
            return video_id, start_dt
        except Exception as e:
            print(f"[⚠️] Could not parse start time: {e}")
            return video_id, None

    print("[❌] No active live stream found.")
    cache["video_id"] = None
    return None, None


# Save clip to local file
def save_clip(title, user, timestamp, url):
    data = {
        "title": title,
        "user": user,
        "timestamp": timestamp,
        "url": url,
        "time": datetime.datetime.now().isoformat()
    }
    if not os.path.exists("clips.json"):
        with open("clips.json", "w") as f:
            json.dump([], f)
    with open("clips.json", "r") as f:
        clips = json.load(f)
    clips.append(data)
    with open("clips.json", "w") as f:
        json.dump(clips, f, indent=2)

# Send clip to Discord
def send_to_discord(title, user, timestamp, url):
    if not DISCORD_WEBHOOK_URL:
        return
    content = f"🎬 **{title}** by `{user}`\n⏱️ Timestamp: `{timestamp}`\n🔗 {url}"
    requests.post(DISCORD_WEBHOOK_URL, json={"content": content})

@app.route("/")
def home():
    return "✅ Clipper is running."

@app.route("/ping")
def ping():
    print("[PING] Self-ping received.")
    return "pong", 200

@app.route("/clip")
def clip():
    user = request.args.get("user", "someone")
    message = request.args.get("message", "").strip()
    title = message if message else "Clip"

    video_id, stream_start = get_cached_live_info()
    if not video_id:
        return "[❌] No active live stream found."
    if not stream_start:
        return "[❌] Couldn’t retrieve stream start time."

    now = datetime.datetime.now(pytz.utc)
    delay = 35  # seconds
    clip_time = now - datetime.timedelta(seconds=delay)
    seconds_since_start = max(0, int((clip_time - stream_start).total_seconds()))
    timestamp_str = str(datetime.timedelta(seconds=seconds_since_start))
    clip_url = f"https://youtu.be/{video_id}?t={seconds_since_start}s"

    save_clip(title, user, timestamp_str, clip_url)
    send_to_discord(title, user, timestamp_str, clip_url)

    return f"[✅] {title} clipped by {user} → {clip_url}"

@app.route("/clips")
def get_clips():
    if not os.path.exists("clips.json"):
        return []
    with open("clips.json") as f:
        return json.load(f)

@app.route("/clear")
def clear_clips():
    open("clips.json", "w").write("[]")
    return "[🗑️] Cleared clips."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
