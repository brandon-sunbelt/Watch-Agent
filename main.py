#!/usr/bin/env python3
"""Watch sourcing agent — single-file build.

Everything lives in this one file on purpose. Run with:  python main.py
"""


# ======================================================================
# MODELS
# ======================================================================
"""Core data model and small shared helpers."""
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Tuple
import hashlib
import re

USER_AGENT = {
    "User-Agent": "watch-agent/1.0 (personal collection tracker; contact: owner)"
}

_PRICE_RE = re.compile(r"(?:USD|US\$|\$|€|£)\s?([0-9]{1,3}(?:[,\.][0-9]{3})*(?:\.[0-9]{2})?)")


def extract_price(text: str) -> Optional[float]:
    """Pull the first plausible price out of free text."""
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    # ignore obvious non-prices (e.g. a stray "$5")
    return val if val >= 100 else None


def extract_links(html: str, base: str = "") -> List[Tuple[str, str]]:
    """Return (url, anchor_text) pairs from an HTML blob."""
    from bs4 import BeautifulSoup
    out = []
    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception:
        soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href or href.startswith("mailto:") or href.startswith("#"):
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/") and base:
            href = base.rstrip("/") + href
        out.append((href, a.get_text(strip=True)))
    return out


@dataclass
class Listing:
    source: str
    title: str
    url: str
    price: Optional[float] = None
    currency: str = "USD"
    condition_text: str = ""
    seller: str = ""
    image: str = ""
    raw_text: str = ""              # title + body, used for matching
    # populated downstream:
    watch_id: Optional[str] = None
    match_score: float = 0.0
    fair_value: Optional[float] = None
    deal_margin: Optional[float] = None
    flags: List[str] = field(default_factory=list)
    grail: bool = False
    status: str = "available"      # available | sold | hold

    @property
    def uid(self) -> str:
        return hashlib.sha1((self.url or self.title).encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["uid"] = self.uid
        return d

# ======================================================================
# CONFIG
# ======================================================================
"""Load and lightly validate watchlist.yaml."""
import os
import yaml


def load_config(path: str = None) -> dict:
    path = path or os.environ.get("WATCHLIST", "watchlist.yaml")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("settings", {})
    cfg.setdefault("sources", {})
    cfg.setdefault("watches", [])
    cfg.setdefault("condition_rules", {})
    cfg.setdefault("scoring", {})
    return cfg


def is_grail(watch: dict) -> bool:
    return (
        watch.get("tier") == 4
        or watch.get("authenticity") == "maximum_scrutiny"
        or bool(watch.get("sourcing_override"))
    )

# ======================================================================
# STATE
# ======================================================================
"""Durable state: which listings we've already reported, and a rolling
price history per watch used to build comparables over time.

The JSON file is committed back to the repo by the GitHub Actions workflow,
so state survives between daily runs without any paid database.
"""
import json
import os
from datetime import datetime, timezone

STATE_PATH = os.environ.get("STATE_PATH", "state/state.json")
MAX_SEEN = 20000
MAX_HISTORY_PER_WATCH = 500


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": [], "price_history": {}}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    state["seen"] = state.get("seen", [])[-MAX_SEEN:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def record_prices(state: dict, listings) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hist = state.setdefault("price_history", {})
    for l in listings:
        if l.watch_id and l.price:
            series = hist.setdefault(l.watch_id, [])
            series.append({"date": today, "price": l.price, "source": l.source, "url": l.url})
    for k in list(hist.keys()):
        hist[k] = hist[k][-MAX_HISTORY_PER_WATCH:]

# ======================================================================
# SOURCES
# ======================================================================
"""Source fetchers. Each returns a list of Listing objects and swallows its
own errors so one dead source never takes down the whole run.

Reliability notes (see README):
  - eBay / Reddit  -> real APIs, robust.
  - Gmail alerts   -> robust (reads Chrono24 + dealer + auction-house alert emails).
  - WatchRecon     -> best-effort HTML scrape; selectors may need tuning.
  - Boutiques      -> tries Shopify products.json first, falls back to HTML;
                      per-site tuning likely needed for some shops.
  - Auctions       -> intentionally light; primary coverage is via auction-house
                      account email alerts ingested through Gmail.
"""
import base64
import os
import imaplib
import email
from email.utils import parseaddr
from datetime import datetime, timezone, timedelta

import requests


TIMEOUT = 30


# --------------------------------------------------------------------------- #
# eBay Browse API
# --------------------------------------------------------------------------- #
EBAY_OAUTH = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH = "https://api.ebay.com/buy/browse/v1/item_summary/search"


def _ebay_token() -> str:
    cid = os.environ["EBAY_CLIENT_ID"]
    secret = os.environ["EBAY_CLIENT_SECRET"]
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = requests.post(
        EBAY_OAUTH,
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_ebay(queries, limit=50):
    listings = []
    try:
        token = _ebay_token()
    except Exception as e:
        print(f"[ebay] auth failed ({e}) — check EBAY_CLIENT_ID / EBAY_CLIENT_SECRET")
        return listings
    headers = {"Authorization": f"Bearer {token}",
               "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    for q in queries:
        try:
            r = requests.get(EBAY_SEARCH, headers=headers,
                             params={"q": q, "limit": limit}, timeout=TIMEOUT)
            r.raise_for_status()
            for it in (r.json().get("itemSummaries") or []):
                p = it.get("price") or {}
                listings.append(Listing(
                    source="eBay",
                    title=it.get("title", ""),
                    url=it.get("itemWebUrl", ""),
                    price=float(p["value"]) if p.get("value") else None,
                    currency=p.get("currency", "USD"),
                    condition_text=it.get("condition", ""),
                    seller=(it.get("seller") or {}).get("username", ""),
                    image=(it.get("image") or {}).get("imageUrl", ""),
                    raw_text=it.get("title", ""),
                ))
        except Exception as e:
            print(f"[ebay] query '{q}' failed: {e}")
    return listings


# --------------------------------------------------------------------------- #
# Reddit (r/Watchexchange) via PRAW, read-only
# --------------------------------------------------------------------------- #
def fetch_reddit(subreddits, limit=100):
    listings = []
    try:
        import praw
    except ImportError:
        print("[reddit] praw not installed")
        return listings
    try:
        reddit = praw.Reddit(
            client_id=os.environ["REDDIT_CLIENT_ID"],
            client_secret=os.environ["REDDIT_CLIENT_SECRET"],
            user_agent=os.environ.get("REDDIT_USER_AGENT", "watch-agent/1.0"),
        )
        reddit.read_only = True
    except Exception as e:
        print(f"[reddit] auth failed ({e}) — check REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET")
        return listings
    for sub in subreddits:
        try:
            for post in reddit.subreddit(sub).new(limit=limit):
                title = post.title or ""
                # Focus on sell posts; matching still filters by reference.
                if "WTS" not in title.upper() and "FS" not in title.upper():
                    continue
                body = post.selftext or ""
                listings.append(Listing(
                    source=f"r/{sub}",
                    title=title,
                    url=f"https://www.reddit.com{post.permalink}",
                    raw_text=f"{title}\n{body}",
                    price=extract_price(title) or extract_price(body),
                ))
        except Exception as e:
            print(f"[reddit] r/{sub} failed: {e}")
    return listings


# --------------------------------------------------------------------------- #
# Gmail alert ingestion (Chrono24 saved-searches, dealer + auction newsletters)
# --------------------------------------------------------------------------- #
def _email_text(msg) -> str:
    if msg.is_multipart():
        html, plain = "", ""
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/html", "text/plain"):
                continue
            try:
                payload = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="ignore")
            except Exception:
                continue
            if ctype == "text/html":
                html += payload
            else:
                plain += payload
        return html or plain
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", errors="ignore")
    except Exception:
        return msg.get_payload() or ""


def fetch_gmail_alerts(senders=None, lookback_days=2):
    """Read recent alert emails. `senders` filters by from-address substring;
    pass an empty list / None to ingest all recent mail and let matching filter."""
    listings = []
    addr = os.environ.get("GMAIL_ADDRESS")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        print("[gmail] no GMAIL_ADDRESS / GMAIL_APP_PASSWORD — skipping alert ingestion")
        return listings
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(addr, pw)
        M.select("INBOX")
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f"(SINCE {since})")
        ids = data[0].split()
        for num in ids[-300:]:
            try:
                typ, msgdata = M.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msgdata[0][1])
            except Exception:
                continue
            frm = parseaddr(msg.get("From", ""))[1].lower()
            if senders and not any(s.lower() in frm for s in senders):
                continue
            subject = msg.get("Subject", "")
            body = _email_text(msg)
            base = ""
            for url, anchor in extract_links(body, base):
                # skip unsubscribe / tracking-only links
                low = url.lower()
                if any(k in low for k in ["unsubscribe", "mailchimp", "list-manage",
                                          "/preferences", "facebook.com", "instagram.com"]):
                    continue
                listings.append(Listing(
                    source=f"Alert: {frm or 'email'}",
                    title=(anchor or subject)[:200],
                    url=url,
                    raw_text=f"{subject}\n{anchor}",
                    price=extract_price(anchor) or extract_price(subject),
                ))
        M.logout()
    except Exception as e:
        print(f"[gmail] failed: {e}")
    return listings


# --------------------------------------------------------------------------- #
# WatchRecon (forum + dealer aggregator) — best-effort scrape
# --------------------------------------------------------------------------- #
def fetch_watchrecon(queries):
    listings = []
    from bs4 import BeautifulSoup
    for q in queries:
        try:
            r = requests.get("https://www.watchrecon.com/", params={"text": q},
                             headers=USER_AGENT, timeout=TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            # TODO: verify these selectors against live markup and tune if needed.
            rows = soup.select("table a[href], .result a[href], li a[href]")
            for a in rows:
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if not href.startswith("http") or len(text) < 8:
                    continue
                if "watchrecon.com" in href and "text=" in href:
                    continue
                listings.append(Listing(
                    source="WatchRecon",
                    title=text[:200],
                    url=href,
                    raw_text=text,
                    price=extract_price(text),
                ))
        except Exception as e:
            print(f"[watchrecon] query '{q}' failed: {e}")
    return listings


# --------------------------------------------------------------------------- #
# Boutique dealer sites — Shopify products.json first, else HTML anchors
# --------------------------------------------------------------------------- #
_PRODUCTish = ("product", "watch", "rolex", "patek", "vacheron", "omega",
               "/shop", "listing", "/watches/")


def fetch_boutiques(boutiques):
    listings = []
    from bs4 import BeautifulSoup
    for b in boutiques:
        url = (b or {}).get("url")
        name = (b or {}).get("name", "boutique")
        if not url:
            continue
        # 1) Shopify structured feed
        try:
            r = requests.get(url.rstrip("/") + "/products.json?limit=250",
                             headers=USER_AGENT, timeout=TIMEOUT)
            if r.status_code == 200 and "products" in r.text:
                for p in r.json().get("products", []):
                    handle = p.get("handle", "")
                    purl = url.rstrip("/") + "/products/" + handle
                    price = None
                    variants = p.get("variants") or []
                    if variants:
                        try:
                            price = float(variants[0].get("price"))
                        except (TypeError, ValueError):
                            price = None
                    sold = bool(variants) and not any(v.get("available") for v in variants)
                    listings.append(Listing(
                        source=name,
                        title=p.get("title", ""),
                        url=purl,
                        price=price,
                        raw_text=f"{p.get('title','')} {(p.get('body_html') or '')[:400]}",
                        status="sold" if sold else "available",
                    ))
                continue  # got structured data; skip HTML fallback
        except Exception:
            pass
        # 2) HTML anchor fallback
        try:
            r = requests.get(url, headers=USER_AGENT, timeout=TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if not text or len(text) < 6:
                    continue
                if not any(k in href.lower() for k in _PRODUCTish):
                    continue
                full = href if href.startswith("http") else url.rstrip("/") + "/" + href.lstrip("/")
                listings.append(Listing(
                    source=name,
                    title=text[:200],
                    url=full,
                    raw_text=text,
                    price=extract_price(text),
                ))
        except Exception as e:
            print(f"[boutique {name}] failed: {e}")
    return listings


# --------------------------------------------------------------------------- #
# Auction houses — light by design (primary coverage is via Gmail alerts)
# --------------------------------------------------------------------------- #
def fetch_auctions(grail_queries, houses):
    """Auction-house sites are JS-heavy and individually structured, so robust
    scraping is unreliable. The recommended (and free) approach is to register
    for each house's account alerts/saved searches and let fetch_gmail_alerts
    ingest them. This stub is left as an extension point."""
    return []

# ======================================================================
# MATCHING
# ======================================================================
"""Match raw listings to the watches on the list, and apply the condition
and budget rules from the config."""
import re


# Matched against normalized text (punctuation already split to spaces), so
# "over-polished" -> "over polished" and the \bpolished\b token still hits,
# while "unpolished" stays a single token and is correctly NOT flagged.
POLISH_RE = re.compile(r"\b(polished|repolished|refinished|recased)\b")
REDIAL_RE = re.compile(r"\b(redial|redialed|aftermarket dial|replacement dial|franken)\b")
_SKIP_REF = {"none", "any", "tbd", ""}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())


def build_queries(cfg) -> list:
    """One search string per watch, preferring brand + reference."""
    qs = []
    for w in cfg["watches"]:
        ref = str(w.get("ref", "")).strip()
        if ref.lower() not in _SKIP_REF:
            qs.append(f"{w['brand']} {ref}")
        else:
            qs.append(f"{w['brand']} {w['model']}")
    # de-dup, preserve order
    return list(dict.fromkeys(qs))


def match_listing(listing, watches):
    """Return the best-matching watch dict, or None. Scores reference matches
    heavily; falls back to brand + model token overlap."""
    text = _norm(f"{listing.raw_text} {listing.title}")
    best, best_score = None, 0.0
    for w in watches:
        score = 0.0
        brand_first = _norm(w["brand"]).split()[0] if w.get("brand") else ""
        if brand_first and brand_first in text:
            score += 1.0
        ref = str(w.get("ref", "")).strip().lower()
        if ref not in _SKIP_REF and _norm(ref) and _norm(ref) in text:
            score += 3.0
        for tok in _norm(w.get("model", "")).split():
            if len(tok) > 3 and tok in text:
                score += 0.5
        for tok in _norm(str(w.get("dial", ""))).split():
            if len(tok) > 3 and tok in text:
                score += 0.3
        if score > best_score:
            best, best_score = w, score
    # require a reference hit, or a solid brand+model signal
    if best and best_score >= 2.0:
        listing.watch_id = best["id"]
        listing.match_score = round(best_score, 2)
        return best
    return None


def apply_rules(listing, watch, cfg):
    text = _norm(f"{listing.raw_text} {listing.title} {listing.condition_text}")
    rules = cfg.get("condition_rules", {})
    listing.grail = is_grail(watch)

    if rules.get("unpolished") == "strongly_preferred":
        if POLISH_RE.search(text):
            listing.flags.append("appears polished")
    if REDIAL_RE.search(text):
        listing.flags.append("possible redial / non-original")

    maxp = watch.get("max_price_usd")
    if not listing.grail and maxp and listing.price and listing.price > maxp:
        listing.flags.append(f"over budget (> ${maxp:,.0f})")

    # availability status (don't override a 'sold' already set from a Shopify feed)
    if listing.status == "available":
        if re.search(r"\bsold\b", text):
            listing.status = "sold"
        elif re.search(r"\b(on hold|reserved|holding)\b", text):
            listing.status = "hold"
    return listing

# ======================================================================
# SCORING
# ======================================================================
"""Estimate fair value from self-built comparables (current run + history)
and attach a deal margin plus authenticity flags."""
import statistics


def fair_value_for(watch_id, this_run, state):
    prices = [l.price for l in this_run if l.watch_id == watch_id and l.price]
    hist = [h["price"] for h in state.get("price_history", {}).get(watch_id, [])
            if h.get("price")]
    pool = prices + hist
    if len(pool) >= 3:
        return statistics.median(pool)
    return None


def score_listing(listing, state, this_run):
    if not listing.watch_id:
        return
    fv = fair_value_for(listing.watch_id, this_run, state)
    listing.fair_value = fv
    if fv and listing.price:
        listing.deal_margin = round((fv - listing.price) / fv, 3)
        if listing.price < 0.5 * fv:
            listing.flags.append("price suspiciously low vs comparables")
    if listing.grail:
        # grail rule: deal-scoring is meaningless; the alert IS the value.
        listing.deal_margin = None
        listing.flags.append("GRAIL — verify authenticity before any action")


def rank(listings):
    """Best first: grails up top, then biggest discount, then most matched."""
    def key(l):
        return (
            0 if l.grail else 1,
            -(l.deal_margin or 0),
            -(l.match_score or 0),
        )
    return sorted(listings, key=key)

# ======================================================================
# DIGEST
# ======================================================================
"""Build the daily email digest and the GitHub Pages dashboard, and send the
email via Gmail SMTP.

Dashboard design: a collector's running catalog. Ink on aged parchment with an
oxidized-brass (verdigris) accent; a characterful serif for watch names and a
monospace ledger face for references, prices and margins. The one signature
element is the "ask vs fair value" gauge on each lot.
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone


def _money(v, cur="USD"):
    if v is None:
        return "—"
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(cur, "")
    return f"{sym}{v:,.0f}"


def _watch_label(cfg, watch_id):
    for w in cfg["watches"]:
        if w["id"] == watch_id:
            ref = w.get("ref", "")
            ref = "" if str(ref).lower() in ("none", "any", "tbd", "") else f" · {ref}"
            return f"{w['brand']} {w['model']}{ref}"
    return watch_id


def _watch_brand(cfg, watch_id):
    for w in cfg["watches"]:
        if w["id"] == watch_id:
            return w.get("brand", "Other")
    return "Other"


# --------------------------------------------------------------------------- #
# Email (inline styles — email clients strip <style> blocks)
# --------------------------------------------------------------------------- #
def build_email(fresh, cfg) -> str:
    n = len(fresh)
    grails = [l for l in fresh if l.grail]
    head = (f"<div style='font-family:Georgia,serif;color:#211d18'>"
            f"<h2 style='margin:0 0 4px'>Watch digest — {datetime.now().strftime('%b %d, %Y')}</h2>"
            f"<p style='margin:0 0 16px;color:#6f6757'>"
            f"{n} new match{'es' if n != 1 else ''}"
            f"{' · ' + str(len(grails)) + ' grail alert(s)' if grails else ''}</p>")
    if not fresh:
        return head + ("<p>No new matches today. The net's still out — "
                       "quiet days mean nothing met your bar, not that nothing ran.</p></div>")
    rows = []
    for l in fresh:
        label = _watch_label(cfg, l.watch_id)
        margin = ""
        if l.deal_margin is not None:
            pct = l.deal_margin * 100
            color = "#3d6b5e" if pct > 0 else "#9b3a2c"
            margin = (f"<span style='color:{color};font-weight:bold'>"
                      f"{pct:+.0f}% vs est. {_money(l.fair_value)}</span>")
        flags = ""
        if l.flags:
            tag = "#9b3a2c" if l.grail or any("low" in f or "polish" in f or "redial" in f
                                              for f in l.flags) else "#a9842f"
            flags = ("<div style='margin-top:4px;font-size:12px;color:" + tag + "'>⚑ "
                     + " · ".join(l.flags) + "</div>")
        rows.append(
            f"<tr><td style='padding:12px 0;border-top:1px solid #ddd6c6'>"
            f"<div style='font-size:12px;color:#8a8170;text-transform:uppercase;"
            f"letter-spacing:.05em'>{label} · {l.source}</div>"
            f"<a href='{l.url}' style='color:#211d18;font-size:16px;text-decoration:none'>"
            f"<strong>{l.title}</strong></a>"
            f"<div style='margin-top:4px'>"
            f"<span style='font-family:monospace;font-size:15px'>{_money(l.price, l.currency)}</span>"
            f"&nbsp;&nbsp;{margin}</div>{flags}</td></tr>"
        )
    return (head + "<table style='width:100%;border-collapse:collapse'>"
            + "".join(rows) + "</table>"
            + "<p style='margin-top:20px;font-size:12px;color:#8a8170'>"
            "Full catalog on your dashboard. Grail prices aren't deal-scored — "
            "the appearance itself is the signal.</p></div>")


# --------------------------------------------------------------------------- #
# Dashboard (full page, GitHub Pages)
# --------------------------------------------------------------------------- #
_DASH_CSS = """
:root{
  --ink:#211d18; --parchment:#e9e3d5; --card:#f5f0e5; --line:#d6cdba;
  --verdigris:#3d6b5e; --brass:#a9842f; --signal:#9b3a2c; --faint:#8a8170;
}
*{box-sizing:border-box}
body{margin:0;background:var(--parchment);color:var(--ink);
  font-family:"Spectral",Georgia,serif;line-height:1.5;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:880px;margin:0 auto;padding:48px 24px 80px}
header{border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:8px}
h1{font-family:"Fraunces","Spectral",serif;font-weight:600;font-size:34px;
  letter-spacing:-.01em;margin:0}
.meta{font-family:"JetBrains Mono",ui-monospace,monospace;font-size:12.5px;
  color:var(--faint);margin-top:8px;display:flex;gap:18px;flex-wrap:wrap}
.meta b{color:var(--ink);font-weight:500}
.section{font-family:"JetBrains Mono",monospace;font-size:11px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--faint);margin:38px 0 6px}
.brand{display:flex;align-items:baseline;justify-content:space-between;
  margin:44px 0 2px;padding-bottom:6px;border-bottom:2px solid var(--ink)}
.brand h2{font-family:"Fraunces","Spectral",serif;font-weight:600;font-size:23px;
  margin:0;letter-spacing:-.01em}
.brand .count{font-family:"JetBrains Mono",monospace;font-size:11.5px;color:var(--faint)}
.new-badge{display:inline-block;font-family:"JetBrains Mono",monospace;font-size:9.5px;
  letter-spacing:.1em;color:var(--parchment);background:var(--verdigris);
  border-radius:3px;padding:1px 5px;margin-left:8px;vertical-align:1px}
.lot{display:grid;grid-template-columns:14px 1fr auto;gap:16px;align-items:start;
  padding:18px 0;border-top:1px solid var(--line)}
.dot{width:9px;height:9px;border-radius:50%;margin-top:8px;background:var(--brass)}
.dot.deal{background:var(--verdigris)} .dot.grail{background:var(--signal)}
.label{font-family:"JetBrains Mono",monospace;font-size:11px;letter-spacing:.05em;
  text-transform:uppercase;color:var(--faint);margin-bottom:2px}
.title{font-size:18px;text-decoration:none;color:var(--ink)}
.title:hover{text-decoration:underline}
.flags{margin-top:6px;font-size:13px;color:var(--signal)}
.flags.soft{color:var(--brass)}
.right{text-align:right;min-width:150px}
.price{font-family:"JetBrains Mono",monospace;font-size:17px}
.margin{font-family:"JetBrains Mono",monospace;font-size:12.5px;margin-top:2px}
.margin.up{color:var(--verdigris)} .margin.down{color:var(--signal)}
/* signature: ask-vs-fair-value gauge */
.gauge{margin-top:8px;height:3px;background:var(--line);position:relative;border-radius:2px}
.gauge .fair{position:absolute;top:-3px;height:9px;width:1px;background:var(--ink);left:50%}
.gauge .ask{position:absolute;top:-2px;height:7px;width:7px;border-radius:50%;
  background:var(--verdigris);transform:translateX(-50%)}
.gauge .ask.over{background:var(--signal)}
.empty{font-family:"Fraunces",serif;font-size:22px;color:var(--faint);
  padding:60px 0;text-align:center}
footer{margin-top:60px;font-family:"JetBrains Mono",monospace;font-size:11px;
  color:var(--faint);border-top:1px solid var(--line);padding-top:16px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:20px 0 0}
.tab{font-family:"JetBrains Mono",monospace;font-size:11.5px;letter-spacing:.03em;
  cursor:pointer;border:1px solid var(--line);background:transparent;color:var(--faint);
  padding:6px 13px;border-radius:999px}
.tab:hover{color:var(--ink);border-color:var(--ink)}
.tab.active{background:var(--ink);color:var(--parchment);border-color:var(--ink)}
.status{display:inline-block;font-family:"JetBrains Mono",monospace;font-size:9.5px;
  letter-spacing:.1em;border-radius:3px;padding:1px 5px;margin-left:8px;vertical-align:1px;
  color:var(--parchment)}
.status.sold{background:var(--signal)} .status.hold{background:var(--brass)}
.lot.is-sold .title{color:var(--faint);text-decoration:line-through}
.section-head{font-family:"Fraunces","Spectral",serif;font-weight:600;font-size:21px;
  margin:48px 0 2px;padding-bottom:6px;border-bottom:2px solid var(--signal)}
.star{cursor:pointer;border:none;background:none;font-size:19px;line-height:1;
  color:var(--brass);padding:0 0 4px;display:block;margin-left:auto}
.star:hover{transform:scale(1.12)}
@media (prefers-reduced-motion:no-preference){.lot{transition:none}}
"""


_DASH_JS = """
const WL='watchagent.watchlist';
function getWL(){try{return new Set(JSON.parse(localStorage.getItem(WL)||'[]'))}catch(e){return new Set()}}
function saveWL(s){localStorage.setItem(WL,JSON.stringify([...s]))}
function syncStars(){const s=getWL();document.querySelectorAll('.star').forEach(b=>{
  const on=s.has(b.dataset.uid);b.textContent=on?'\\u2605':'\\u2606';b.classList.toggle('on',on);});}
function toggleWatch(btn){const s=getWL();const id=btn.dataset.uid;
  s.has(id)?s.delete(id):s.add(id);saveWL(s);syncStars();
  const a=document.querySelector('.tab.active');if(a&&a.dataset.filter==='watchlist')applyFilter('watchlist');}
function applyFilter(f){
  const avail=document.getElementById('available'),sold=document.getElementById('soldhold'),
        wlEmpty=document.getElementById('wl-empty');
  avail.style.display='none';sold.style.display='none';wlEmpty.style.display='none';
  document.querySelectorAll('.brand-block,.lot,.brand,.section-head').forEach(e=>e.style.display='');
  if(f==='all'){avail.style.display='';}
  else if(f==='sold'){sold.style.display='';}
  else if(f.indexOf('brand:')===0){avail.style.display='';const br=f.slice(6);
    document.querySelectorAll('.brand-block').forEach(b=>{b.style.display=(b.dataset.brand===br)?'':'none';});}
  else if(f==='watchlist'){const s=getWL();let any=false;avail.style.display='';sold.style.display='';
    document.querySelectorAll('.brand,.section-head').forEach(h=>h.style.display='none');
    document.querySelectorAll('.lot').forEach(l=>{const show=s.has(l.dataset.uid);l.style.display=show?'':'none';if(show)any=true;});
    document.querySelectorAll('.brand-block').forEach(b=>{
      const vis=[...b.querySelectorAll('.lot')].some(l=>l.style.display!=='none');b.style.display=vis?'':'none';});
    if(!any){avail.style.display='none';sold.style.display='none';wlEmpty.style.display='';}}
}
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');applyFilter(t.dataset.filter);}));
syncStars();
"""


def _gauge(listing):
    if listing.fair_value and listing.price:
        # position ask 0..100 across a window of 0.5x..1.5x fair value
        ratio = listing.price / listing.fair_value
        pos = max(2, min(98, (ratio - 0.5) / 1.0 * 100))
        over = "over" if ratio > 1 else ""
        return (f"<div class='gauge'><span class='fair'></span>"
                f"<span class='ask {over}' style='left:{pos:.0f}%'></span></div>")
    return ""


def _lot_html(l, cfg, is_new=False):
    label = _watch_label(cfg, l.watch_id)
    brand = _watch_brand(cfg, l.watch_id)
    status = getattr(l, "status", "available")
    dot = "grail" if l.grail else ("deal" if (l.deal_margin or 0) > 0.05 else "")
    badge = "<span class='new-badge'>NEW</span>" if is_new else ""
    pill = ""
    if status == "sold":
        pill = "<span class='status sold'>SOLD</span>"
    elif status == "hold":
        pill = "<span class='status hold'>ON HOLD</span>"
    margin = ""
    if l.deal_margin is not None:
        cls = "up" if l.deal_margin > 0 else "down"
        margin = (f"<div class='margin {cls}'>{l.deal_margin*100:+.0f}% vs "
                  f"{_money(l.fair_value)}</div>")
    flags = ""
    if l.flags:
        hard = l.grail or any(k in f for f in l.flags for k in ("low", "polish", "redial"))
        flags = f"<div class='flags{'' if hard else ' soft'}'>⚑ {' · '.join(l.flags)}</div>"
    sold_cls = " is-sold" if status == "sold" else ""
    return (
        f"<div class='lot{sold_cls}' data-brand='{brand}' data-uid='{l.uid}'>"
        f"<span class='dot {dot}'></span>"
        f"<div><div class='label'>{label} · {l.source}</div>"
        f"<a class='title' href='{l.url}' target='_blank' rel='noopener'>{l.title}</a>{pill}{badge}"
        f"{flags}</div>"
        f"<div class='right'>"
        f"<button class='star' data-uid='{l.uid}' onclick='toggleWatch(this)' title='Save to watchlist'>☆</button>"
        f"<div class='price'>{_money(l.price, l.currency)}</div>"
        f"{margin}{_gauge(l)}</div></div>"
    )


def build_dashboard(fresh, all_matched, cfg, state, out_path="docs/index.html"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n_watch = len(cfg["watches"])
    fresh_uids = {l.uid for l in fresh}

    catalog = {}
    for l in all_matched:
        catalog.setdefault(l.uid, l)
    items = list(catalog.values())
    available = [l for l in items if getattr(l, "status", "available") == "available"]
    soldhold = [l for l in items if getattr(l, "status", "available") in ("sold", "hold")]

    # group available by brand, most-listed brand first
    groups = {}
    for l in available:
        groups.setdefault(_watch_brand(cfg, l.watch_id), []).append(l)
    brand_order = [b for b, _ in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))]

    # tabs
    tabs = ["<button class='tab active' data-filter='all'>All</button>"]
    for b in brand_order:
        tabs.append(f"<button class='tab' data-filter='brand:{b}'>{b}</button>")
    tabs.append("<button class='tab' data-filter='watchlist'>★ Watchlist</button>")
    if soldhold:
        tabs.append("<button class='tab' data-filter='sold'>Sold / Hold</button>")

    # available area (brand sections, priced low to high)
    avail = []
    if groups:
        for b in brand_order:
            lst = sorted(groups[b], key=lambda l: (l.price is None, l.price or 0))
            n_new = sum(1 for l in lst if l.uid in fresh_uids)
            count = f"{len(lst)} listing{'s' if len(lst) != 1 else ''}"
            if n_new:
                count += f" · {n_new} new"
            avail.append(f"<div class='brand-block' data-brand='{b}'>"
                         f"<div class='brand'><h2>{b}</h2><span class='count'>{count}</span></div>")
            for l in lst:
                avail.append(_lot_html(l, cfg, is_new=l.uid in fresh_uids))
            avail.append("</div>")
    else:
        avail.append("<div class='empty'>Catalog's empty for now.<br>The net's still out.</div>")

    # sold / on-hold area (separate section, hidden until its tab is chosen)
    sold = []
    if soldhold:
        sold.append("<div class='section-head'>Sold &amp; on hold</div>")
        for l in sorted(soldhold, key=lambda l: (_watch_brand(cfg, l.watch_id),
                                                 l.price is None, l.price or 0)):
            sold.append(_lot_html(l, cfg, is_new=l.uid in fresh_uids))

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watch Catalog</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Spectral:wght@400;500&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{_DASH_CSS}</style></head>
<body><div class="wrap">
<header><h1>The Catalog</h1>
<div class="meta"><span>Run <b>{ts}</b></span>
<span>Watching <b>{n_watch}</b> references</span>
<span>Available <b>{len(available)}</b></span>
<span>New today <b>{len(fresh)}</b></span></div>
<nav class="tabs">{''.join(tabs)}</nav></header>
<div id="available">{''.join(avail)}</div>
<div id="soldhold" style="display:none">{''.join(sold)}</div>
<div id="wl-empty" class="empty" style="display:none">Nothing saved yet.<br>Tap a ☆ on any listing to start your watchlist.</div>
<footer>Personal sourcing agent · grouped by brand, priced low to high · sold &amp; held items
moved to their own tab · grail references alert on any appearance and aren't deal-scored ·
prices are listed asks, fair value is a self-built comparables median.</footer>
</div>
<script>{_DASH_JS}</script>
</body></html>"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


# --------------------------------------------------------------------------- #
# Send
# --------------------------------------------------------------------------- #
def send_email(html, cfg):
    addr = os.environ.get("GMAIL_ADDRESS")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("RECIPIENT_EMAIL", addr)
    if not addr or not pw:
        print("[email] no Gmail creds — digest not sent (dashboard still updated)")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Watch digest — {datetime.now().strftime('%b %d')}"
    msg["From"] = addr
    msg["To"] = to
    msg.attach(MIMEText("View the HTML digest in a capable client.", "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(addr, pw)
            s.sendmail(addr, [to], msg.as_string())
        print(f"[email] digest sent to {to}")
    except Exception as e:
        print(f"[email] send failed: {e}")

# ======================================================================
# MAIN
# ======================================================================
"""Entry point. Run with:  python main.py"""


def run():
    cfg = load_config()
    state = load_state()
    watches = cfg["watches"]
    queries = build_queries(cfg)
    src = cfg.get("sources", {})

    raw = []
    if src.get("ebay", {}).get("enabled"):
        raw += fetch_ebay(queries)
    if src.get("reddit", {}).get("enabled"):
        raw += fetch_reddit(src["reddit"].get("subreddits", ["Watchexchange"]))
    if src.get("watchrecon", {}).get("enabled"):
        raw += fetch_watchrecon(queries)
    if src.get("chrono24", {}).get("enabled") or src.get("auction_houses", {}).get("enabled"):
        raw += fetch_gmail_alerts(senders=None)
    if src.get("boutiques"):
        raw += fetch_boutiques(src["boutiques"])

    print(f"[run] collected {len(raw)} raw listings from all sources")

    matched = []
    for l in raw:
        w = match_listing(l, watches)
        if w:
            apply_rules(l, w, cfg)
            matched.append(l)
    print(f"[run] {len(matched)} matched the watchlist")

    seen = set(state.get("seen", []))
    fresh, seen_now = [], set()
    for l in matched:
        if l.uid in seen or l.uid in seen_now:
            continue
        seen_now.add(l.uid)
        fresh.append(l)
    print(f"[run] {len(fresh)} are new today")

    # score the whole current catalog so every brand section shows margins
    for l in matched:
        score_listing(l, state, matched)

    record_prices(state, matched)
    state["seen"] = list(seen | seen_now)

    build_dashboard(fresh, matched, cfg, state)
    if fresh:
        send_email(build_email(fresh, cfg), cfg)

    save_state(state)
    print("[run] done")


if __name__ == "__main__":
    run()
