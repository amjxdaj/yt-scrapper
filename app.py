
import os
import requests
import datetime as dt
from dateutil import parser as dtparse
from dateutil import tz
import streamlit as st
import re  
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# -----------------------------
# Helpers
# -----------------------------

def iso_to_seconds(iso):
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    h = int(m.group(1) or 0); m_ = int(m.group(2) or 0); s = int(m.group(3) or 0)
    return h*3600 + m_*60 + s

def chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

# -----------------------------
# Channel resolver (handle/URL -> channel ID)
# -----------------------------

def resolve_to_channel_id(api_key: str, input_str: str) -> str:
    """Accepts a channel ID, handle like @marvel, or any YouTube channel URL
    and returns the canonical channel ID (UCxxxxxxxxxxxx...)."""
    s = (input_str or "").strip()
    if not s:
        raise ValueError("Empty channel input.")

    # Already a channel ID?
    if re.fullmatch(r"UC[0-9A-Za-z_-]{22}", s):
        return s

    # URL forms
    if s.startswith("http://") or s.startswith("https://"):
        # /channel/UC...
        m = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", s)
        if m:
            return m.group(1)

        # /@handle
        m = re.search(r"/@([A-Za-z0-9_.-]+)", s)
        if m:
            handle = "@" + m.group(1)
            return channel_id_from_handle(api_key, handle)

        # /user/LegacyName
        m = re.search(r"/user/([A-Za-z0-9]+)", s)
        if m:
            return channel_id_from_username(api_key, m.group(1))

        # /c/CustomName or other custom URL
        m = re.search(r"/c/([A-Za-z0-9_.-]+)", s)
        if m:
            return channel_id_from_search(api_key, m.group(1))

        # Fallback: last path piece
        tail = s.rstrip("/").split("/")[-1]
        return channel_id_from_search(api_key, tail)

    # Plain handle like @marvel
    if s.startswith("@"):
        return channel_id_from_handle(api_key, s)

    # Plain legacy username or custom name
    return channel_id_from_username(api_key, s)

def channel_id_from_handle(api_key, handle):
    url = "https://www.googleapis.com/youtube/v3/channels"
    r = requests.get(url, params={"part": "id", "forHandle": handle, "key": api_key}).json()
    items = r.get("items", [])
    if not items:
        raise ValueError(f"Handle not found: {handle}")
    return items[0]["id"]

def channel_id_from_username(api_key, username):
    url = "https://www.googleapis.com/youtube/v3/channels"
    r = requests.get(url, params={"part": "id", "forUsername": username, "key": api_key}).json()
    items = r.get("items", [])
    if not items:
        # Fallback to search if legacy username fails
        return channel_id_from_search(api_key, username)
    return items[0]["id"]

def channel_id_from_search(api_key, query):
    url = "https://www.googleapis.com/youtube/v3/search"
    r = requests.get(url, params={
        "part": "snippet",
        "type": "channel",
        "q": query,
        "maxResults": 1,
        "key": api_key
    }).json()
    items = r.get("items", [])
    if not items:
        raise ValueError(f"No channel found for query: {query}")
    return items[0]["id"]["channelId"]

# -----------------------------
# API wrappers
# -----------------------------

def get_uploads_playlist_id(api_key, channel_id):
    url = "https://www.googleapis.com/youtube/v3/channels"
    r = requests.get(url, params={
        "part": "contentDetails",
        "id": channel_id,
        "key": api_key
    })
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        raise ValueError("Channel not found or API key/channel ID invalid.")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

def list_upload_video_ids(api_key, uploads_playlist_id, max_items=200):
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    ids = []
    token = None
    while True:
        r = requests.get(url, params={
            "part": "contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": 50,
            "pageToken": token,
            "key": api_key
        })
        r.raise_for_status()
        data = r.json()
        ids.extend([it["contentDetails"]["videoId"] for it in data.get("items", [])])
        token = data.get("nextPageToken")
        if not token or len(ids) >= max_items:
            break
    return ids[:max_items]

def fetch_video_details(api_key, video_ids):
    if not video_ids:
        return []
    url = "https://www.googleapis.com/youtube/v3/videos"
    out = []
    for group in chunk(video_ids, 50):
        r = requests.get(url, params={
            "part": "snippet,contentDetails,statistics",
            "id": ",".join(group),
            "key": api_key
        })
        r.raise_for_status()
        data = r.json()
        for it in data.get("items", []):
            stats = it.get("statistics", {})
            snippet = it.get("snippet", {})
            thumbs = snippet.get("thumbnails", {})
            # pick a decent thumbnail
            thumb = (thumbs.get("maxres") or thumbs.get("standard") or thumbs.get("high") or 
                     thumbs.get("medium") or thumbs.get("default") or {}).get("url")
            out.append({
                "id": it["id"],
                "title": snippet.get("title", ""),
                "url": f"https://www.youtube.com/watch?v={it['id']}",
                "publishedAt": dtparse.parse(snippet.get("publishedAt")) if snippet.get("publishedAt") else None,
                "duration_seconds": iso_to_seconds(it["contentDetails"]["duration"]),
                "likes": int(stats.get("likeCount", 0)) if stats.get("likeCount", "0").isdigit() else 0,
                "comments": int(stats.get("commentCount", 0)) if stats.get("commentCount", "0").isdigit() else 0,
                "thumbnail": thumb,
            })
    return out

def in_range(ts, start, end):
    if ts is None:
        return False
    return (ts >= start) and (ts < end)

def compute_kpis(videos, start, end, short_threshold_sec=60):
    items = [v for v in videos if in_range(v["publishedAt"], start, end)]
    videos_only = [v for v in items if v["duration_seconds"] > short_threshold_sec]
    shorts_only = [v for v in items if v["duration_seconds"] <= short_threshold_sec]

    def top_by(key, pool):
        return max(pool, key=lambda x: x.get(key, 0)) if pool else None

    return {
        "items": items,
        "count_total": len(items),
        "count_videos": len(videos_only),
        "count_shorts": len(shorts_only),
        "most_liked": top_by("likes", items),
        "most_commented": top_by("comments", items),
    }

# -----------------------------
# UI
# -----------------------------

st.set_page_config(page_title="YouTube Ops Dashboard", layout="wide")
st.title("📊 YouTube Ops Dashboard")

with st.sidebar:
    st.header("Setup")
    api_key = os.getenv("YOUTUBE_API_KEY", "")
    if not api_key:
        st.error("❌ No API key found. Please set YOUTUBE_API_KEY in your .env file.")
        st.stop()

    raw_input = st.text_input("Channel handle or URL")
    max_items = st.slider("Max uploads to scan (most recent)", min_value=50, max_value=1000, value=300, step=50)
    short_threshold = st.number_input("Shorts threshold (seconds ≤)", min_value=15, max_value=180, value=60, step=5)
    tz_local = tz.tzlocal()
    st.caption("Tip: Add env var YT_API_KEY to avoid pasting every time.")

if not api_key or not raw_input:
    st.info("Enter API key and a channel handle/URL/ID in the left sidebar to begin.")
    st.stop()

# Resolve to channel ID
try:
    channel_id = resolve_to_channel_id(api_key, raw_input)
    st.success(f"Resolved Channel ID: {channel_id}")
except Exception as e:
    st.error(f"Channel resolve error: {e}")
    st.stop()

@st.cache_data(show_spinner=True, ttl=3600)
def load_channel_data(api_key, channel_id, max_items):
    uploads = get_uploads_playlist_id(api_key, channel_id)
    ids = list_upload_video_ids(api_key, uploads, max_items=max_items)
    details = fetch_video_details(api_key, ids)
    # sort newest first
    details.sort(key=lambda x: x["publishedAt"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    return details

try:
    videos = load_channel_data(api_key, channel_id, max_items)
except Exception as e:
    st.error(f"Error loading channel data: {e}")
    st.stop()

col_search = st.columns([3,1,1,1,1])

with col_search[0]:
    st.text_input("Search here..", placeholder="Filter by title keyword (optional)", key="search_kw")
with col_search[1]:
    st.markdown("&nbsp;")
    st.metric("Total Loaded", len(videos))
with col_search[2]:
    st.markdown("&nbsp;")
    st.metric("Videos (>{}s)".format(short_threshold), len([v for v in videos if v["duration_seconds"] > short_threshold]))
with col_search[3]:
    st.markdown("&nbsp;")
    st.metric("Shorts (≤{}s)".format(short_threshold), len([v for v in videos if v["duration_seconds"] <= short_threshold]))
with col_search[4]:
    st.markdown("&nbsp;")
    st.metric("Time Zone", str(dt.datetime.now(tz_local).tzname()))

# Date ranges
now_utc = dt.datetime.now(dt.timezone.utc)
today_start_local = dt.datetime.now(tz_local).replace(hour=0, minute=0, second=0, microsecond=0)
today_start = today_start_local.astimezone(dt.timezone.utc)
month_start_local = today_start_local.replace(day=1)
month_start = month_start_local.astimezone(dt.timezone.utc)

# Custom dates
st.subheader("Date Range Filters")
c1, c2, c3 = st.columns([1,1,2])
with c1:
    custom_start = st.date_input("Custom Start", value=dt.datetime.now().date().replace(day=1))
with c2:
    custom_end = st.date_input("Custom End", value=dt.datetime.now().date())
with c3:
    st.write(" ")

custom_start_dt = dt.datetime.combine(custom_start, dt.time(0,0,0), tzinfo=tz_local).astimezone(dt.timezone.utc)
custom_end_dt = dt.datetime.combine(custom_end, dt.time(23,59,59), tzinfo=tz_local).astimezone(dt.timezone.utc)

# Optional keyword filter
kw = st.session_state.get("search_kw", "").strip().lower()
videos_filtered = [v for v in videos if kw in v["title"].lower()] if kw else videos

# KPI sections
def kpi_row(title, start, end):
    k = compute_kpis(videos_filtered, start, end, short_threshold_sec=short_threshold)
    colA, colB, colC = st.columns(3)
    with colA:
        st.markdown("### "+title)
        st.metric("No. of Posts", k["count_total"])
        if start == today_start and k["count_total"] == 0:
            st.error("🚨 No Post Today — Go & Do Post")
    with colB:
        ml = k["most_liked"]
        st.markdown("**Most Liked**")
        if ml:
            if ml["thumbnail"]:
                st.image(ml["thumbnail"], use_container_width=True)
            st.markdown(f"[{ml['title']}]({ml['url']})")
            st.caption(f"👍 {ml['likes']} · 💬 {ml['comments']}")
        else:
            st.caption("No items in range.")
    with colC:
        mc = k["most_commented"]
        st.markdown("**Most Commented**")
        if mc:
            if mc["thumbnail"]:
                st.image(mc["thumbnail"], use_container_width=True)
            st.markdown(f"[{mc['title']}]({mc['url']})")
            st.caption(f"💬 {mc['comments']} · 👍 {mc['likes']}")
        else:
            st.caption("No items in range.")

st.divider()
kpi_row("Today", today_start, now_utc)
st.divider()
kpi_row("This Month", month_start, now_utc)
st.divider()
kpi_row("Custom Range", custom_start_dt, custom_end_dt)

st.caption("Note: 'Shorts' are inferred as videos with duration ≤ threshold (default 60s). YouTube Data API does not expose a direct 'shares' metric; for 'Most Shared', use the YouTube Analytics API with OAuth.")
