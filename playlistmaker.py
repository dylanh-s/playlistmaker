"""playlistmaker — build Apple Music playlists from NTS / Spotify tracklists.

Backend: the Apple Music *web* API (amp-api.music.apple.com), driven with your
own logged-in session — no developer account, no Spotify API. Two tokens are
needed and both are obtained automatically:

  • developer token — a public JWT Apple ships to every web client; scraped from
    the music.apple.com JS bundle.
  • media-user-token — your personal session token; read from your Firefox
    cookie store (so: stay signed into Apple Music in Firefox).

This replaced the old AppleScript/Music.app UI automation, which was
non-deterministic and broke on albums, stations and special characters. The old
version is kept as playlistmaker_old.py.
"""

import argparse
import glob
import html as html_mod
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata

import requests

WEB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TAG_RE = re.compile(r"<[^>]+>")
SPOTIFY_ID_RE = re.compile(r'(?:playlist[/:])([A-Za-z0-9]+)')


def clean(s):
    """Strip nested tags, unescape entities, collapse whitespace."""
    s = TAG_RE.sub("", s)
    s = html_mod.unescape(s)
    return " ".join(s.split())


# =============================================================================
# Tracklist ingestion  (unchanged behaviour — this part always worked)
# =============================================================================

def spotify_embed_url(url):
    m = SPOTIFY_ID_RE.search(url)
    if not m:
        raise ValueError(f"Could not find a playlist id in {url!r}")
    return f"https://open.spotify.com/embed/playlist/{m.group(1)}"


def _find_tracklist(node):
    """Walk the JSON for the first dict carrying a trackList. The exact path
    moves between deploys; the shape doesn't."""
    if isinstance(node, dict):
        if isinstance(node.get("trackList"), list) and node["trackList"]:
            return node
        for v in node.values():
            found = _find_tracklist(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_tracklist(v)
            if found:
                return found
    return None


def parse_html_spotify(html) -> tuple[str, list[dict]]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        raise ValueError(
            "No __NEXT_DATA__ — you've probably hit open.spotify.com "
            "rather than the embed endpoint"
        )
    entity = _find_tracklist(json.loads(m.group(1)))
    if not entity:
        raise ValueError("__NEXT_DATA__ found but no trackList inside it")

    name = clean(entity.get("name") or entity.get("title") or "Imported Playlist")
    tracks = []
    for item in entity["trackList"]:
        title = clean(item.get("title", ""))
        artist = clean(item.get("subtitle", ""))
        if artist:
            artist = artist.split(",")[0].strip()  # primary artist only
        if title:
            tracks.append({"title": title, "artist": artist})
    return name, tracks


def parse_html(html) -> tuple[str, list[dict]]:
    # NTS embeds full episode state as JSON in a <script> tag
    match = re.search(r'window\._REACT_STATE_\s*=\s*({.*?});\s*</script>', html, re.DOTALL)
    if not match:
        raise ValueError("Warning: could not find _REACT_STATE_ in page")

    state = json.loads(match.group(1))
    episode = state.get("episode", {})
    show_name = episode.get("name", "Unknown Show")
    date = episode.get("broadcast_formatted", "")
    playlist_name = f"{show_name} – {date}" if date else show_name

    tracks = []
    for t in episode.get("tracklist", []):
        artists = t.get("mainArtists", [])
        artist = ", ".join(a["name"] for a in artists) if artists else "Unknown Artist"
        tracks.append({"title": t.get("title", "Unknown Title"), "artist": artist})
    return playlist_name, tracks


def get_html(url):
    try:
        response = requests.get(url, headers={"User-Agent": WEB_UA}, timeout=10)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")
        return None


def get_html_spotify(url):
    return get_html(spotify_embed_url(url))


# =============================================================================
# Search-query cleanup
# =============================================================================
# Catalog search matches better without decorations the tracklist carries along.
_PAREN_JUNK_RE = re.compile(
    r"""\s*[\(\[]\s*(?:
        feat\.?|featuring|ft\.?|with|
        remaster(?:ed)?|remix|
        mono|stereo|live|
        deluxe|bonus|edit|version|
        original\s+mix|radio\s+edit
    ).*?[\)\]]""",
    re.IGNORECASE | re.VERBOSE,
)
_DASH_SUFFIX_RE = re.compile(
    r"\s+-\s+(?:.*?remaster.*|.*?remix.*|.*?\blive\b.*|.*?version.*)$",
    re.IGNORECASE,
)


def clean_query(title, artist):
    """Build a lean, catalog-friendly search string from title + artist."""
    t = _PAREN_JUNK_RE.sub("", title or "")
    t = _DASH_SUFFIX_RE.sub("", t)
    t = " ".join(t.split()).strip()
    a = (artist or "").split(",")[0].strip()  # primary artist only
    return f"{t} {a}".strip()


# =============================================================================
# Token acquisition  (dev token: scraped; user token: Firefox cookie)
# =============================================================================

TOKENS_PATH = os.path.expanduser("~/.playlistmaker_tokens.json")
_JWT_RE = re.compile(r'eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+')


def _read_user_token_from_firefox():
    """The media-user-token lives in Firefox's cookies.sqlite for
    .music.apple.com. Copy the DB (it may be live) and read it out."""
    base = os.path.expanduser("~/Library/Application Support/Firefox/Profiles")
    for db in glob.glob(os.path.join(base, "*", "cookies.sqlite")):
        tmp = tempfile.mktemp(suffix=".sqlite")
        try:
            shutil.copy(db, tmp)
            if os.path.exists(db + "-wal"):
                shutil.copy(db + "-wal", tmp + "-wal")
            con = sqlite3.connect(tmp)
            row = con.execute(
                "SELECT value FROM moz_cookies "
                "WHERE host LIKE '%music.apple.com%' AND name='media-user-token' "
                "ORDER BY length(value) DESC LIMIT 1"
            ).fetchone()
            con.close()
            if row and row[0]:
                return row[0]
        except Exception:
            continue
        finally:
            for p in (tmp, tmp + "-wal", tmp + "-shm"):
                if os.path.exists(p):
                    os.remove(p)
    return None


def _scrape_developer_token():
    """Pull the public developer JWT out of the web player's JS bundle."""
    s = requests.Session()
    s.headers["User-Agent"] = WEB_UA
    html = s.get("https://music.apple.com/us/browse", timeout=20).text
    for path in re.findall(r'src="(/assets/[^"]+\.js)"', html):
        try:
            js = s.get("https://music.apple.com" + path, timeout=20).text
        except requests.RequestException:
            continue
        m = _JWT_RE.search(js)
        if m:
            return m.group(0)
    return None


def refresh_tokens():
    dev = _scrape_developer_token()
    user = _read_user_token_from_firefox()
    if not dev or not user:
        raise RuntimeError(
            "Token refresh failed "
            f"(developer={'ok' if dev else 'MISSING'}, "
            f"user={'ok' if user else 'MISSING — sign into Apple Music in Firefox'})."
        )
    data = {"developer_token": dev, "user_token": user}
    with open(TOKENS_PATH, "w") as f:
        json.dump(data, f)
    os.chmod(TOKENS_PATH, 0o600)
    return data


def load_tokens(force=False):
    if not force and os.path.exists(TOKENS_PATH):
        try:
            return json.load(open(TOKENS_PATH))
        except (json.JSONDecodeError, OSError):
            pass
    return refresh_tokens()


# =============================================================================
# Apple Music web API client
# =============================================================================

BASE = "https://amp-api.music.apple.com"


class Music:
    def __init__(self):
        self.tok = load_tokens()
        self.storefront = None
        self._refreshed = False  # only auto-refresh tokens once per run

    def _headers(self, extra=None):
        h = {
            "Authorization": f"Bearer {self.tok['developer_token']}",
            "Media-User-Token": self.tok["user_token"],
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "User-Agent": WEB_UA,
        }
        if extra:
            h.update(extra)
        return h

    def _req(self, method, path, headers=None, **kw):
        url = BASE + path if path.startswith("/") else path
        r = requests.request(method, url, headers=self._headers(headers), timeout=25, **kw)
        if r.status_code in (401, 403) and not self._refreshed:
            # tokens likely stale — refresh once and retry
            self._refreshed = True
            self.tok = refresh_tokens()
            r = requests.request(method, url, headers=self._headers(headers), timeout=25, **kw)
        return r

    def resolve_storefront(self):
        if self.storefront:
            return self.storefront
        r = self._req("GET", "/v1/me/storefront")
        r.raise_for_status()
        self.storefront = r.json()["data"][0]["id"]
        return self.storefront

    def search_songs(self, term, limit=5):
        sf = self.resolve_storefront()
        r = self._req("GET", f"/v1/catalog/{sf}/search",
                      params={"term": term, "types": "songs", "limit": str(limit)})
        if not r.ok:
            return []
        return r.json().get("results", {}).get("songs", {}).get("data", [])

    def find_library_playlist(self, name):
        want = name.strip().lower()
        offset = 0
        while True:
            r = self._req("GET", "/v1/me/library/playlists",
                          params={"limit": "100", "offset": str(offset)})
            if not r.ok:
                return None
            j = r.json()
            items = j.get("data", [])
            for p in items:
                if p.get("attributes", {}).get("name", "").strip().lower() == want:
                    return p["id"]
            if not j.get("next") or not items:
                return None
            offset += len(items)

    def create_playlist(self, name, description=""):
        r = self._req("POST", "/v1/me/library/playlists",
                      headers={"Content-Type": "application/json"},
                      data=json.dumps({"attributes": {"name": name, "description": description}}))
        r.raise_for_status()
        return r.json()["data"][0]["id"]

    def existing_catalog_ids(self, pid):
        """Catalog song IDs already in the playlist, so we never add a dup.
        Library tracks carry their source catalog id in playParams.catalogId."""
        ids = set()
        offset = 0
        while True:
            r = self._req("GET", f"/v1/me/library/playlists/{pid}/tracks",
                          params={"limit": "100", "offset": str(offset)})
            if not r.ok:
                break
            items = r.json().get("data", [])
            for t in items:
                pp = t.get("attributes", {}).get("playParams", {})
                cid = pp.get("catalogId") or pp.get("purchasedId")
                if cid:
                    ids.add(str(cid))
            if len(items) < 100:
                break
            offset += len(items)
        return ids

    def add_tracks(self, pid, song_ids):
        """Add catalog songs to a library playlist. One request for the batch."""
        if not song_ids:
            return True
        r = self._req("POST", f"/v1/me/library/playlists/{pid}/tracks",
                      headers={"Content-Type": "application/json"},
                      data=json.dumps({"data": [{"id": i, "type": "songs"} for i in song_ids]}))
        if not r.ok:
            print(f"   add failed: HTTP {r.status_code} {r.text[:200]}")
        return r.ok


# =============================================================================
# Match confidence — honour "skip + report", never add the wrong thing
# =============================================================================

def _norm_words(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))  # drop accents
    s = re.sub(r"[^\w ]", " ", s.lower(), flags=re.UNICODE)     # keep CJK + alnum
    return set(w for w in s.split() if w)


def _is_non_latin(s):
    """True if the string is written in a non-Latin script (CJK, Cyrillic,
    Arabic, …). Accented Latin (Björk, Sigur Rós) normalises to ASCII → False."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return any(ch.isalpha() and ord(ch) > 127 for ch in s)


def _continuous(s):
    """Lowercased letters/digits/CJK only, no spaces or punctuation — so CJK
    titles that differ only by punctuation/spacing compare equal
    (螞蟻螞蟻 vs 螞蟻、螞蟻)."""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[^\w]", "", s.lower())


def is_confident(q_title, q_artist, r_title, r_artist, rank):
    """Decide whether result `rank` (0 = Apple's top hit) is the query track.

    Latin-title queries use a strict artist + title-word gate. Non-Latin titles
    (CJK, …) are special: the GB catalog frequently *translates* the title to
    English (艷陽天 → "Sunny Days") or romanises the artist (万能青年旅店 →
    "Omnipotent Youth Society"), so we (a) accept when the CJK title survives and
    matches as a continuous string, else (b) trust Apple's #1 result when the
    artist agrees — Apple's search matched the original title internally."""
    qt, qa = _norm_words(q_title), _norm_words(q_artist)
    rt, ra = _norm_words(r_title), _norm_words(r_artist)
    artist_ok = (not qa) or bool(qa & ra)

    if _is_non_latin(q_title):
        qc, rc = _continuous(q_title), _continuous(r_title)
        if len(qc) >= 2 and (qc in rc or rc in qc):
            return True                      # original title preserved & matches
        if rank == 0 and (artist_ok or _is_non_latin(q_artist)):
            # Title was translated. Trust Apple's top hit when the artist agrees,
            # OR when the query's own artist is non-Latin — Apple then romanises
            # it (粉紅A → "Fan Hung A") so a literal mismatch is expected, not a
            # red flag. A romanised query artist that *doesn't* match, by
            # contrast, means Apple fell back to unrelated fuzzy results → skip.
            return True
        return False

    # Latin title: at least half the query words present, and artist agrees.
    title_half = (not qt) or len(qt & rt) >= max(1, (len(qt) + 1) // 2)
    return artist_ok and title_half


def pick_match(track, results):
    for rank, s in enumerate(results):
        a = s.get("attributes", {})
        if is_confident(track["title"], track["artist"],
                        a.get("name", ""), a.get("artistName", ""), rank):
            return s
    return None


# =============================================================================
# Orchestration
# =============================================================================

def build_playlist(playlist_name, tracks):
    mc = Music()
    sf = mc.resolve_storefront()
    print(f"Storefront: {sf}")

    pid = mc.find_library_playlist(playlist_name)
    if pid:
        print(f"Using existing playlist “{playlist_name}” ({pid})")
        already = mc.existing_catalog_ids(pid)
        if already:
            print(f"  ({len(already)} track(s) already in playlist — will skip dups)")
    else:
        pid = mc.create_playlist(playlist_name, "Imported by playlistmaker")
        print(f"Created playlist “{playlist_name}” ({pid})")
        already = set()

    seen = set()          # dedup within this run
    ids, failed, dups = [], [], 0
    for t in tracks:
        term = clean_query(t["title"], t["artist"])
        results = mc.search_songs(term)
        pick = pick_match(t, results)
        if not pick:
            failed.append(t)
            why = "no results" if not results else "no confident match"
            print(f"  ✗ {t['title']} — {t['artist']}  ({why})")
            continue
        a = pick["attributes"]
        sid = str(pick["id"])
        if sid in already or sid in seen:
            dups += 1
            print(f"  ↷ {t['title']} — {t['artist']}  (already in playlist, skipped)")
            continue
        seen.add(sid)
        ids.append(sid)
        print(f"  ✓ {t['title']} — {t['artist']}  →  {a.get('name')} — {a.get('artistName')}")

    ok = mc.add_tracks(pid, ids)
    added = len(ids) if ok else 0
    print(f"\n✅ Done! Added {added}/{len(tracks)} to “{playlist_name}”.")
    if dups:
        print(f"   ({dups} duplicate(s) skipped)")
    if failed:
        print("\n⚠️  Not added (search these manually):")
        for t in failed:
            print(f"   • {t['title']} — {t['artist']}")


# =============================================================================
# Input handlers + CLI
# =============================================================================

def handle_nts(url=None):
    url = url or input("Paste an NTS URL: ").strip()
    html = get_html(url)
    if html:
        return parse_html(html)
    raise SystemExit("Error: failed to fetch NTS url")


def handle_spotify(url=None):
    url = url or input("Paste a Spotify URL: ").strip()
    html = get_html_spotify(url)
    if html:
        return parse_html_spotify(html)
    raise SystemExit("Error: failed to fetch Spotify url")


def main():
    parser = argparse.ArgumentParser(
        description="Build an Apple Music playlist from an NTS or Spotify tracklist.",
        epilog=(
            "examples:\n"
            "  python3 playlistmaker.py --mode spotify --path https://open.spotify.com/playlist/<id>\n"
            "  python3 playlistmaker.py --mode nts --path https://www.nts.live/shows/<...>\n"
            "  python3 playlistmaker.py --search \"aphex twin windowlicker\"\n"
            "  python3 playlistmaker.py --refresh-tokens\n\n"
            "Requires: Python 3, `requests`, and being signed into Apple Music in Firefox."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mode", choices=["nts", "spotify"], help="Source of the tracklist")
    parser.add_argument("--path", help="URL for the chosen mode")
    parser.add_argument("--refresh-tokens", action="store_true", help="Re-scrape/re-read the Apple Music tokens and exit")
    parser.add_argument("--search", metavar="TERM", help="Diagnostic: search the catalog for TERM and print the top matches, then exit")
    args = parser.parse_args()

    if args.refresh_tokens:
        refresh_tokens()
        print(f"✓ Tokens refreshed → {TOKENS_PATH}")
        return

    if args.search:
        mc = Music()
        for s in mc.search_songs(args.search):
            a = s["attributes"]
            print(f"  {a.get('name')} — {a.get('artistName')}  (id {s['id']})")
        return

    mode = args.mode
    if not mode:
        mode = input("Enter mode ('nts' or 'spotify'): ").strip().lower()
        while mode not in ("nts", "spotify"):
            mode = input("Invalid. Enter 'nts' or 'spotify': ").strip().lower()

    if mode == "nts":
        playlist_name, tracks = handle_nts(url=args.path)
    else:
        playlist_name, tracks = handle_spotify(url=args.path)

    print(f"\nPlaylist: {playlist_name}")
    print(f"Tracks: {len(tracks)}\n")
    build_playlist(playlist_name, tracks)


if __name__ == "__main__":
    main()
