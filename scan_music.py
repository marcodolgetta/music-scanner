#!/usr/bin/env python3
"""
New Music Scanner
------------------
Scans a curated list of music-blog RSS feeds, pulls out new-release posts,
guesses the artist name and genre for each one, and writes everything to
output.json (a running history, deduplicated by link).

Run it manually:
    python3 scan_music.py

Or schedule it (recommended: daily or every few days) with cron. Example,
to run every day at 8am:
    crontab -e
    0 8 * * * /usr/bin/python3 /path/to/scan_music.py >> /path/to/scan_music.log 2>&1

Then open dashboard.html and load output.json to browse results, grouped
by genre and artist.
"""

import json
import re
import os
import calendar
import http.client
import time
from datetime import datetime, timedelta, timezone

import feedparser

# feedparser's default User-Agent literally announces itself as
# "UniversalFeedParser/x.x +https://pypi.org/project/feedparser/" — an
# extremely recognizable bot signature that some sites (openrss.org
# included, it seems) block outright. A normal browser UA avoids that.
feedparser.USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Only keep posts from roughly the last month. Anything older gets skipped
# during the scan (it's still safe to re-run daily/weekly — old posts just
# won't make it into output.json going forward).
MAX_AGE_DAYS = 30

# YouTube's channel RSS feed mixes Shorts in with regular uploads — there's
# no separate feed for them. Set to False to include Shorts anyway (or
# override per-channel with "skip_shorts": False in a YOUTUBE_FEEDS entry).
SKIP_YOUTUBE_SHORTS = True


# --- Feed sources -----------------------------------------------------
# "genre" here is only the fallback used if the post's title/summary don't
# match any known genre keyword (see classify_genres below). We don't rely
# on any blog's RSS <category> tags for genre — most reuse that field for
# tagging artists/topics mentioned in a post (e.g. "Foo Fighters", "News"),
# not musical genre.
#
# "max_items" caps how many entries we take from a single feed per scan.
# Most blog feeds only ever return their ~20-30 most recent posts, but
# some feeds (like KEXP's, a ~20-year archive) return far more, which can
# drown out every other source. Leave it unset (None) for feeds that are
# naturally small already.
FEEDS = [
    {"url": "https://pitchfork.com/rss/news/", "source": "Pitchfork", "genre": "Indie/General", "homepage": "https://pitchfork.com/news/"},
    {"url": "https://www.stereogum.com/feed/", "source": "Stereogum", "genre": "Indie/Alternative", "homepage": "https://www.stereogum.com/"},
    {"url": "https://www.brooklynvegan.com/feed/", "source": "BrooklynVegan", "genre": "Indie/Punk", "homepage": "https://www.brooklynvegan.com/"},
    {"url": "https://www.nme.com/news/music/feed", "source": "NME", "genre": "General", "homepage": "https://www.nme.com/news/music"},
    {"url": "https://feeds.feedburner.com/TheFaderMagazine", "source": "The Fader", "genre": "Hip-Hop/Pop", "homepage": "https://www.thefader.com/"},
    {"url": "https://consequence.net/feed/", "source": "Consequence", "genre": "General", "homepage": "https://consequence.net/"},
    {"url": "https://diymag.com/feed", "source": "DIY Magazine", "genre": "Indie", "homepage": "https://diymag.com/"},
    {"url": "https://assets.complex.com/feeds/channels/music.xml", "source": "Complex Music", "genre": "Hip-Hop", "homepage": "https://www.complex.com/music"},
    {"url": "https://www.rollingstone.com/music/feed/", "source": "Rolling Stone", "genre": "General", "homepage": "https://www.rollingstone.com/music/"},
    {"url": "https://www.clashmusic.com/feed/", "source": "Clash", "genre": "General", "homepage": "https://www.clashmusic.com/"},
    {"url": "https://www.loudandquiet.com/feed/", "source": "Loud And Quiet", "genre": "Indie/Alternative", "homepage": "https://www.loudandquiet.com/"},
    {"url": "https://uproxx.com/music/feed/", "source": "Uproxx Music", "genre": "Hip-Hop/Pop", "homepage": "https://uproxx.com/music/"},
    # Bandcamp dropped native RSS entirely, so this leans on openrss.org, a
    # free third-party feed generator that scrapes the page on request.
    # If it ever goes down, swap in a self-hosted RSS-Bridge Bandcamp
    # bridge instead: https://github.com/RSS-Bridge/rss-bridge
    {"url": "https://openrss.org/daily.bandcamp.com", "source": "Bandcamp Daily", "genre": "Various/Bandcamp Picks", "style": "bandcamp", "homepage": "https://daily.bandcamp.com"},

    # --- Record labels (via each label's own Bandcamp page) ---------------
    # Same openrss.org approach as Bandcamp Daily above, pointed at each
    # label's page instead of the editorial site. Artist name isn't reliably
    # guessable from a label page's release titles, so these use the same
    # "bandcamp" style (skips artist guessing, tags as a roundup).
    {"url": "https://openrss.org/subpop.bandcamp.com", "source": "Sub Pop", "genre": "Indie", "style": "bandcamp", "homepage": "https://subpop.bandcamp.com"},
    {"url": "https://openrss.org/matadorrecords.bandcamp.com", "source": "Matador Records", "genre": "Indie", "style": "bandcamp", "homepage": "https://matadorrecords.bandcamp.com"},
    {"url": "https://openrss.org/mergerecords.bandcamp.com", "source": "Merge Records", "genre": "Indie", "style": "bandcamp", "homepage": "https://mergerecords.bandcamp.com"},
    {"url": "https://openrss.org/warprecords.bandcamp.com", "source": "Warp Records", "genre": "Electronic/Dance", "style": "bandcamp", "homepage": "https://warprecords.bandcamp.com"},
    {"url": "https://openrss.org/ninjatune.bandcamp.com", "source": "Ninja Tune", "genre": "Electronic/Dance", "style": "bandcamp", "homepage": "https://ninjatune.bandcamp.com"},
    {"url": "https://openrss.org/xlrecordings.bandcamp.com", "source": "XL Recordings", "genre": "Indie/Electronic", "style": "bandcamp", "homepage": "https://xlrecordings.bandcamp.com"},
    {"url": "https://openrss.org/4adofficial.bandcamp.com", "source": "4AD", "genre": "Indie/Alternative", "style": "bandcamp", "homepage": "https://4adofficial.bandcamp.com"},
    {"url": "https://openrss.org/dominorecordco.bandcamp.com", "source": "Domino Recording Co.", "genre": "Indie", "style": "bandcamp", "homepage": "https://dominorecordco.bandcamp.com"},
    {"url": "https://openrss.org/mellomusicgroup.bandcamp.com", "source": "Mello Music Group", "genre": "Hip-Hop/Rap", "style": "bandcamp", "homepage": "https://mellomusicgroup.bandcamp.com"},
    {"url": "https://openrss.org/massappealrecs.bandcamp.com", "source": "Mass Appeal", "genre": "Hip-Hop/Rap", "style": "bandcamp", "homepage": "https://massappealrecs.bandcamp.com"},
    # NOTE: TDE and pgLang don't appear to have a Bandcamp page or public
    # RSS feed at all — their artists release through major distribution,
    # not an indie storefront. YouTube is realistically the only reliable
    # public feed for them; see the YOUTUBE_FEEDS section below.

    # RA's podcast is a weekly artist mix series rather than news, but it's
    # useful electronic-music coverage the blogs above barely touch.
    {"url": "https://ra.co/xml/podcast.xml", "source": "RA Podcast", "genre": "Electronic/Dance", "style": "ra_podcast", "homepage": "https://ra.co/podcast", "max_items": 10},
    # KEXP's daily new-music pick, distributed as a podcast feed via Omny.
    # This feed spans ~20 years of episodes, and some of the oldest ones
    # still point at KEXP's pre-2016 media hosting, which is long gone.
    # Capped hard, since otherwise it drowns out every other source.
    {"url": "https://www.omnycontent.com/d/playlist/bad5d079-8dcb-4630-8770-aa090049131d/32b2ac38-5a48-4300-9fa6-aa40002038b5/4ac1c451-4315-4096-ab9b-aa40002038c4/podcast.rss", "source": "KEXP (In Our Headphones)", "genre": "Eclectic/Radio Pick", "style": "kexp", "homepage": "https://www.kexp.org/podcasts/song-of-the-day/", "max_items": 15},
]

# --- YouTube channels ---------------------------------------------------
# Every YouTube channel has a free RSS feed — no API key needed — but it
# requires the channel's actual ID (looks like "UCxxxxxxxxxxxxxxxxxxxxxx"),
# not its @handle. To get one: open the channel, click "..." on the About
# page (or the channel description) > Share channel > Copy channel ID.
# Or use a free lookup tool like https://ytlarge.com/channel-id-finder
#
# Fill in channel_id below for each channel you want tracked, then delete
# the leading "#" to enable that entry. I've left the ones you asked about
# (COLORS, Tiny Desk, KEXP's video channel, TDE, pgLang, plus your own
# artists) as placeholders since guessing a channel ID wrong means silently
# tracking the wrong channel rather than a visible error.
YOUTUBE_FEEDS = [
     {"channel_id": "UC2Qw1dzXDBAZPwS7zm37g8g", "source": "COLORS", "genre": "Various/Live Session", "style": "youtube"},
     {"channel_id": "UC4eYXhJI4-7wSWc8UNRwD4A", "source": "NPR Music (Tiny Desk)", "genre": "Various/Live Session", "style": "youtube"},
     {"channel_id": "UC3I2GFN_F8WudD_2jUZbojA", "source": "KEXP (YouTube)", "genre": "Eclectic/Radio Pick", "style": "youtube"},
     {"channel_id": "UCBY8aDToI-OBq2fe7NbwoqA", "source": "TDE", "genre": "Hip-Hop/Rap", "style": "youtube"},
     {"channel_id": "UCZwYLLsXM2rBtixxFAdYR1A", "source": "pgLang", "genre": "Hip-Hop/Rap", "style": "youtube"},
     {"channel_id": "UCo2IatrwKyvABUmGbSA473Q", "source": "All Them Witches", "genre": "Folk Rock", "style": "youtube"},
     {"channel_id": "UCGBpxWJr9FNOcFYA5GkKrMg", "source": "Boiler Room", "genre": "Electronic", "style": "youtube"},
     {"channel_id": "UCDe08Fs0s0YKJuk5h45csAQ", "source": "Polyphia", "genre": "Technical Metal", "style": "youtube"},
     {"channel_id": "UCHVbuwA78fcrsESJtKGz7dg", "source": "Rhymesayers Entertainment", "genre": "Rap/Hip-Hop", "style": "youtube"},
     {"channel_id": "UCgzSMg9C9YnVKPcnT3f9PDw", "source": "Will On The Soul.", "genre": "Rap/Hip-Hop", "style": "youtube"},
     {"channel_id": "UC8TZwtZ17WKFJSmwTZQpBTA", "source": "MAJ", "genre": "DJ Setlist", "style": "youtube"},
     {"channel_id": "UCMkBFD0YPtrcoB_tni5uOLQ", "source": "RyanCelsiusSounds", "genre": "DJ Setlist", "style": "youtube"},
     {"channel_id": "UCLmaR7ew57x0XJEe_-REUyg", "source": "Book Club Radio", "genre": "DJ Setlist", "style": "youtube"},
     {"channel_id": "UCt7fwAhXDy3oNFTAzF2o8Pw", "source": "theneedledrop", "genre": "music review", "style": "youtube"},
     {"channel_id": "UCD-4g5w1h8xQpLaNS_ghU4g", "source": "NewRetroWave", "genre": "NewRetroWave", "style": "youtube"},
     {"channel_id": "UCW7MRMCxD5dbOU7TQaCAMLQ", "source": "Saddle Creek", "genre": "indie rock", "style": "youtube"},
     {"channel_id": "UCexxwLQxN5KTTvhwJuwZZWg", "source": "Method Records and Music", "genre": "Label", "style": "youtube"},
     {"channel_id": "UCGgf_k1y-_d-4Eh922iCCJg", "source": "Interscope Records", "genre": "Label", "style": "youtube"},
     {"channel_id": "UCsgEkEWaXKQwrhlLHFbcQFw", "source": "Sub Pop", "genre": "Label", "style": "youtube"},
     {"channel_id": "UCzVC0z-KheQEV_2H2zg6V9w", "source": "Aesop Rock", "genre": "Rap", "style": "youtube"},
     {"channel_id": "UCFAKGci5lneha2x4XMbzYrQ", "source": "Blogothèque", "genre": "Various/Live Session", "style": "youtube"},
     {"channel_id": "UCRUOfuNIb_sk__7snjK3aVg", "source": "ElFamosoDemon", "genre": "Various", "style": "youtube"},
    # {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "source": "Some Artist", "genre": "General", "style": "youtube"},
    # {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "source": "Some Artist", "genre": "General", "style": "youtube"},
    # {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "source": "Some Artist", "genre": "General", "style": "youtube"},
    # {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "source": "Some Artist", "genre": "General", "style": "youtube"},
    # {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "source": "Some Artist", "genre": "General", "style": "youtube"},
    # {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "source": "Some Artist", "genre": "General", "style": "youtube"},
    # {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "source": "Some Artist", "genre": "General", "style": "youtube"},
    # {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "source": "Some Artist", "genre": "General", "style": "youtube"},
    # {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "source": "Some Artist", "genre": "General", "style": "youtube"},
    # {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "source": "Some Artist", "genre": "General", "style": "youtube"},
]


def build_feed_url(feed: dict) -> str:
    if feed.get("url"):
        return feed["url"]
    if feed.get("channel_id"):
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={feed['channel_id']}"
    raise ValueError(f"Feed {feed.get('source')} has neither 'url' nor 'channel_id'")


ALL_FEEDS = FEEDS + [f for f in YOUTUBE_FEEDS if f.get("channel_id")]

# Verbs music blogs use in headlines when announcing a new release, e.g.
# "Artist Name Shares New Single 'Title'". Used to guess the artist.
RELEASE_VERBS = (
    r"share[sd]?|releases?|drops?|announces?|unveils?|premieres?|teases?|"
    r"reveals?|returns? with|debuts?|delivers?|preps?|prepares?|readies?|ready|"
    r"confirms?|signs? (?:to|with)?|previews?|plots?|details?|sets?|"
    r"schedules?|gears? up for|kicks? off|launch(?:es)?|"
    r"surprises? (?:us|fans)? with|"
    r"covers?|samples?|joins?|recruits?|taps?|enlists?|"
    r"forms?|start(?:s)?|team(?:s)? up(?: with)?|"
    r"(?:is |are )?(?:set )?to play|(?:is |are )?(?:set )?to headline"
)
ARTIST_PATTERN = re.compile(rf"^(.*?)\s+(?:{RELEASE_VERBS})\b", re.IGNORECASE)

# KEXP/podcast-style write-ups tend to name the artist in the description
# rather than the headline, e.g.: `...the song "2SIDED" is from Arlo Parks'
# latest album...` or `"Skyline" by Overmono, out now on XL Recordings.`
DESC_ARTIST_PATTERNS = [
    re.compile(r"[\u201c\"]([^\u201d\"]{2,80})[\u201d\"]\s+by\s+([A-Z][\w&.,'\u2019\- ]{1,50}?)(?:,|\.|\s+from|\s+off|\s+on\b|$)"),
    re.compile(r"[\u201c\"]([^\u201d\"]{2,80})[\u201d\"]\s+(?:is\s+)?from\s+([A-Z][\w&.,'\u2019\- ]{1,50}?)(?:'|\u2019)s\b"),
]

# RA Podcast titles are just "RA.1043 Pretty Girl" — episode number, then
# the artist name.
RA_PODCAST_PATTERN = re.compile(r"^RA\.?\d+\s*[:\-]?\s*(.+)$", re.IGNORECASE)

# Lots of outlets title cover/single posts as "Artist - Song" or
# "Artist – Song" with no verb at all, e.g. KEXP's "Dead Bars - Lucky" or
# Stereogum's "Courtney Marie Andrews – 'Carolina Caroline' (Cover)".
TITLE_DASH_PATTERN = re.compile(r"^([A-Za-z0-9][\w&.,'\u2019 ]{1,50}?)\s*[-\u2013\u2014]\s*.+$")

# Live-session YouTube channels often title videos "Artist: Show Name", e.g.
# NPR Tiny Desk's "Le Sserafim: Tiny Desk Concert". Only applied for
# style="youtube" — a colon is too common in blog headlines ("Review: ...")
# to safely assume it always precedes an artist name.
TITLE_COLON_PATTERN = re.compile(r"^([A-Za-z0-9][\w&.,'\u2019 ]{1,50}?):\s+.+$")

# Bandcamp Daily headlines are usually just the article/list title (e.g.
# "Best Ambient on Bandcamp: June 2026"), so there's rarely a single artist.
BANDCAMP_SKIP_ARTIST = "Various Artists (roundup)"

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output.json")


def guess_artist(title: str, summary: str = "", style: str = "") -> str:
    if style == "bandcamp":
        return BANDCAMP_SKIP_ARTIST

    if style == "ra_podcast":
        m = RA_PODCAST_PATTERN.match(title.strip())
        if m:
            artist = m.group(1).strip(" \"'\u201c\u201d")
            if 0 < len(artist) <= 60:
                return artist

    match = ARTIST_PATTERN.match(title.strip())
    if match:
        artist = match.group(1).strip(" \"'\u201c\u201d")
        if 0 < len(artist) <= 60:
            return artist

    dash_match = TITLE_DASH_PATTERN.match(title.strip())
    if dash_match:
        artist = dash_match.group(1).strip(" \"'\u201c\u201d")
        if 0 < len(artist) <= 60:
            return artist

    if style == "youtube":
        colon_match = TITLE_COLON_PATTERN.match(title.strip())
        if colon_match:
            artist = colon_match.group(1).strip(" \"'\u201c\u201d")
            if 0 < len(artist) <= 60:
                return artist

    if style == "kexp" and summary:
        for pattern in DESC_ARTIST_PATTERNS:
            m = pattern.search(summary)
            if m:
                artist = m.group(2).strip(" \"'\u201c\u201d.,")
                if 0 < len(artist) <= 60:
                    return artist

    return "Unknown"


# Music blogs often reuse the <category> field for things that aren't
# genres at all: the artist's own name, or site-navigation labels like
# "News" and "Reviews". Rather than trust any blog's tags, we classify
# genre from the post's own title/summary text against known genre
# vocabulary. Order matters a little: more specific terms are checked
# before broad ones so e.g. "post-punk" doesn't just register as "pop".
GENRE_KEYWORDS = [
    ("Hip-Hop/Rap", ["hip-hop", "hip hop", "rapper", "rap "]),
    ("R&B/Soul", ["r&b", "rnb", "neo-soul", "soul"]),
    ("Post-Punk", ["post-punk", "post punk"]),
    ("Punk", ["punk", "hardcore"]),
    ("Metal", ["metal", "thrash", "doom", "sludge", "black metal", "death metal"]),
    ("Emo", ["emo"]),
    ("Shoegaze/Dream Pop", ["shoegaze", "dream pop", "dream-pop"]),
    ("Grunge", ["grunge"]),
    ("Electronic/Dance", [
        "electronic", "edm", "house music", " techno", "trance", "dubstep",
        "drum and bass", "d&b", "jungle", "garage", "grime", "idm",
        "breakbeat", "downtempo", "trip-hop",
    ]),
    ("Hyperpop", ["hyperpop", "digicore", "glitchcore"]),
    ("Ambient/Experimental", ["ambient", "experimental", "noise", "drone"]),
    ("Folk/Americana", ["folk", "americana", "bluegrass", "singer-songwriter"]),
    ("Country", ["country"]),
    ("Jazz", ["jazz"]),
    ("Classical", ["classical", "orchestral", "opera"]),
    ("Reggae/Ska", ["reggae", "dub ", "ska"]),
    ("Latin", ["latin", "reggaeton", "salsa", "cumbia", "corrido"]),
    ("Afrobeat/Amapiano", ["afrobeat", "amapiano", "afrobeats"]),
    ("K-Pop/J-Pop", ["k-pop", "kpop", "j-pop", "jpop"]),
    ("New Wave/Synth", ["new wave", "synth-pop", "synthpop", "darkwave"]),
    ("Pop", [" pop ", " pop.", " pop,", " pop\"", "pop star"]),
    ("Alternative/Indie", ["indie", "alternative", "alt-rock"]),
    ("Rock", ["rock"]),
]


def classify_genres(text: str) -> list:
    text_l = f" {text.lower()} "
    hits = []
    for label, keywords in GENRE_KEYWORDS:
        if any(kw in text_l for kw in keywords):
            hits.append(label)
    return hits


def guess_genres(entry, fallback: str) -> list:
    title = getattr(entry, "title", "") or ""
    summary = getattr(entry, "summary", "") or ""
    hits = classify_genres(f"{title} {summary}")
    if hits:
        return hits
    return [fallback]


AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac")


def _is_audio_url(url: str) -> bool:
    if not url:
        return False
    return url.lower().split("?")[0].endswith(AUDIO_EXTENSIONS)


def resolve_link(entry, homepage: str) -> str:
    """Some podcast-style feeds (looking at you, 20-year-old KEXP archive)
    put a dead raw-audio URL in <link> instead of the episode webpage.
    Try the normal link first, then any alternate HTML link on the entry,
    then the entry's guid if it's a real URL, then fall back to the feed's
    homepage so we never hand out a link we already know is broken."""
    link = getattr(entry, "link", None)
    if link and not _is_audio_url(link):
        return link

    for candidate in getattr(entry, "links", []) or []:
        href = candidate.get("href")
        rel = candidate.get("rel", "")
        ctype = candidate.get("type", "")
        if href and not _is_audio_url(href) and rel != "enclosure" and "audio" not in ctype:
            return href

    guid = getattr(entry, "id", None)
    if guid and guid.startswith("http") and not _is_audio_url(guid):
        return guid

    return homepage


def extract_youtube_video_id(entry) -> str:
    """YouTube's Atom feed includes <yt:videoId>, which feedparser exposes
    as entry.yt_videoid. Fall back to pulling it out of the watch URL."""
    vid = getattr(entry, "yt_videoid", None)
    if vid:
        return vid
    link = getattr(entry, "link", "") or ""
    m = re.search(r"[?&]v=([\w-]{6,})", link)
    return m.group(1) if m else ""


def is_youtube_short(video_id: str, timeout: int = 6) -> bool:
    """YouTube's /shorts/{id} URL loads normally (200) if the video really
    is a Short, but redirects to the regular /watch page otherwise. That
    difference is the only reliable way to tell them apart without the
    paid Data API. Any error/timeout is treated as 'not a Short' — better
    to occasionally let one through than to drop a real upload."""
    if not video_id:
        return False
    try:
        conn = http.client.HTTPSConnection("www.youtube.com", timeout=timeout)
        conn.request("HEAD", f"/shorts/{video_id}")
        status = conn.getresponse().status
        conn.close()
        return status == 200
    except (OSError, http.client.HTTPException):
        return False


def parsed_entry_time(entry):
    """Returns a timezone-aware datetime for the entry's published/updated
    date, or None if there's nothing parseable."""
    struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not struct:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def is_recent(entry, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """True if the entry was published within the last max_age_days.
    If there's no parseable date at all, we let it through rather than
    silently dropping it — better to see an undated post than lose it."""
    entry_time = parsed_entry_time(entry)
    if entry_time is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return entry_time >= cutoff


def load_existing() -> dict:
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_run": None, "entries": []}


def scan() -> dict:
    data = load_existing()
    seen_links = {e["link"] for e in data["entries"]}
    new_count = 0

    for feed in ALL_FEEDS:
        feed_url = build_feed_url(feed)

        # openrss.org seems to be sensitive to rapid back-to-back requests
        # (every Bandcamp-backed feed failed identically in one run) — a
        # small pause between hits to it is cheap insurance against that,
        # on top of the real-browser User-Agent set above.
        if "openrss.org" in feed_url:
            time.sleep(2)

        parsed = feedparser.parse(feed_url)

        # YouTube's videos.xml endpoint has a well-documented, unresolved
        # habit of returning 404/500 for completely valid, active channels
        # — a known server-side quirk (see
        # https://discuss.ai.google.dev/t/youtube-rss-feed-endpoint-returns-404-errors/113379),
        # not a sign of a bad channel_id. It tends to clear up within
        # minutes, so a couple of retries with a short pause is worth it
        # before giving up on a YouTube feed for this run.
        if feed.get("style") == "youtube":
            attempts = 1
            while len(parsed.entries) == 0 and attempts < 3:
                time.sleep(5)
                parsed = feedparser.parse(feed_url)
                attempts += 1

        status = getattr(parsed, "status", None)
        raw_count = len(parsed.entries)
        if parsed.get("bozo") and raw_count == 0:
            print(f"[WARN] {feed['source']}: parse issue, no entries recovered "
                  f"(status={status}, error={parsed.get('bozo_exception')})")
        elif raw_count == 0:
            print(f"[WARN] {feed['source']}: feed returned 0 entries "
                  f"(status={status}, url={feed_url})")
        elif parsed.get("bozo"):
            print(f"[OK]   {feed['source']}: {raw_count} entries returned "
                  f"(status={status}, note: feed has a minor XML quirk but parsed fine)")
        else:
            print(f"[OK]   {feed['source']}: {raw_count} entries returned "
                  f"(status={status})")

        max_items = feed.get("max_items")
        entries = parsed.entries[:max_items] if max_items else parsed.entries
        for entry in entries:
            link = resolve_link(entry, feed.get("homepage", feed_url))
            title = getattr(entry, "title", None)
            if not link or not title or link in seen_links:
                continue
            if not is_recent(entry):
                continue
            if feed.get("style") == "youtube" and feed.get("skip_shorts", SKIP_YOUTUBE_SHORTS):
                if is_youtube_short(extract_youtube_video_id(entry)):
                    continue

            published = getattr(entry, "published", None) or getattr(entry, "updated", None)
            entry_time = parsed_entry_time(entry)
            summary = getattr(entry, "summary", "") or ""
            artist = guess_artist(title, summary, feed.get("style", ""))

            record = {
                "title": title,
                "link": link,
                "published": published,
                "published_iso": entry_time.isoformat() if entry_time else None,
                "source": feed["source"],
                "genres": guess_genres(entry, feed["genre"]),
                "artist": artist,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            }
            data["entries"].append(record)
            seen_links.add(link)
            new_count += 1

    # Prune anything that's aged out of the one-month window since it was
    # first stored (keeps output.json from growing forever, and keeps the
    # dashboard scoped to "new music from the last month" as it ages).
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    kept = []
    for e in data["entries"]:
        iso = e.get("published_iso")
        if iso:
            try:
                if datetime.fromisoformat(iso) < cutoff:
                    continue
            except ValueError:
                pass
        kept.append(e)
    data["entries"] = kept

    data["last_run"] = datetime.now(timezone.utc).isoformat()
    # Keep newest first
    data["entries"].sort(key=lambda e: e.get("scanned_at", ""), reverse=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Scan complete: {new_count} new entries. Total stored: {len(data['entries'])}.")
    print(f"Written to {OUTPUT_PATH}")
    return data


if __name__ == "__main__":
    scan()
