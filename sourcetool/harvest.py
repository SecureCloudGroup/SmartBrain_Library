"""`sourcetool harvest`: pull candidate sources from open catalogs of catalogs (US scope, v1).

Catalog fetches are documented API calls (api=True: provider API terms govern, see docs/POLICY.md).

Every harvested record lands with tier "harvested" in sources/harvested/<name>.jsonl, classified into the
taxonomy by keywords (a draft; review can move it). Harvest never overwrites curated records.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from .common import SOURCES, get, get_json, now_iso, read_jsonl, slug, write_jsonl
from .schema import classify, validate_record

HARVESTERS = ["socrata", "mobility", "gbfs", "apis_guru", "public_apis", "wikidata_feeds", "census"]

US_STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
             "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
             "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR"}


def _rec(id, name, desc, provider, cats, kinds, geo, kind, url, *, auth="none", terms="unverified", terms_note="",
         cadence="irregular", docs=None, examples=(), entity="", origin_ref="", signals=None, params=()):
    return {
        "id": id, "name": name[:160], "description": (desc or name)[:600],
        "provider": provider, "tier": "harvested", "categories": cats or ["civic/open_data"], "kinds": kinds,
        "coverage": {"geo": geo, "entity": entity},
        "access": {"kind": kind, "url_template": url, "params": list(params), "auth": auth, "headers": {},
                   "docs_url": docs or url},
        "terms": {"status": terms, "note": terms_note, "terms_url": docs or ""},
        "freshness": {"cadence": cadence}, "examples": list(examples), "notes": "",
        "origin": {"by": "harvest", "at": now_iso(), "ref": origin_ref}, "signals": signals or {},
        "validation": {"status": "unvalidated"}, "votes": {"yes": 0, "no": 0},
    }


def _clean(html: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _cadence_from_updated(updated: str | None) -> str:
    if not updated:
        return "irregular"
    age = datetime.now(timezone.utc) - datetime.fromisoformat(updated.replace("Z", "+00:00"))
    return "daily" if age < timedelta(days=2) else "weekly" if age < timedelta(days=10) else \
        "monthly" if age < timedelta(days=40) else "quarterly" if age < timedelta(days=120) else "annual"


# ----------------------------------------------------------------------------------- Socrata open data
def socrata(max_rows: int = 10000, min_views: int = 30) -> list[dict]:
    """Most-used US government datasets on Socrata portals (cities, states, federal: CDC, CMS...)."""
    out, seen = [], set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=400)
    for off in range(0, max_rows, 100):
        d = get_json(f"https://api.us.socrata.com/api/catalog/v1?only=dataset&order=page_views_last_month"
                     f"&limit=100&offset={off}", cache_hours=72, api=True)
        res = d.get("results", [])
        if not res:
            break
        for x in res:
            r, m, cl = x["resource"], x.get("metadata", {}), x.get("classification", {})
            dom = m.get("domain", "")
            views = (r.get("page_views") or {}).get("page_views_last_month", 0)
            upd = r.get("data_updated_at") or r.get("updatedAt")
            if views < min_views or not dom or not upd:
                continue
            if datetime.fromisoformat(upd.replace("Z", "+00:00")) < cutoff:
                continue
            if dom.endswith((".ca", ".uk", ".au", ".nz", ".eu", ".mx", ".br", ".fr", ".de", ".it", ".es", ".nl",
                             ".ie", ".in", ".za", ".jp", ".sg")):
                continue  # the US catalog also lists some non-US portals; v1 scope is the US
            rid = slug("socrata", dom.replace("data.", ""), r["id"])
            if rid in seen:
                continue
            seen.add(rid)
            cats_txt = " ".join([r["name"], _clean(r.get("description"))[:400], cl.get("domain_category") or "",
                                 " ".join(cl.get("domain_tags") or []), " ".join(cl.get("categories") or [])])
            cats = classify(cats_txt) or ["civic/open_data"]
            if "civic/open_data" not in cats:
                cats.append("civic/open_data")
            cols = r.get("columns_field_name") or []
            out.append(_rec(
                rid, r["name"], _clean(r.get("description"))[:600],
                {"id": slug("socrata", dom), "name": r.get("attribution") or dom, "url": f"https://{dom}",
                 "authority": "official"},
                cats[:4], ["latest_items", "count", "lookup"], "US", "http_json",
                f"https://{dom}/resource/{r['id']}.json?$limit=50",
                terms="open_license" if m.get("license") else "unverified", terms_note=m.get("license") or "",
                cadence=_cadence_from_updated(upd), docs=x.get("permalink") or x.get("link"),
                entity=dom, origin_ref="api.us.socrata.com/catalog",
                signals={"views_month": views, "updated": upd, "columns": cols[:40]}))
    return out


# ----------------------------------------------------------------------------------- Mobility Database
def mobility() -> list[dict]:
    """US transit feeds (GTFS schedules and GTFS-realtime) from MobilityData's catalog."""
    _, body, _ = get("https://storage.googleapis.com/storage/v1/b/mdb-csv/o/sources.csv?alt=media", cache_hours=72, api=True)
    out = []
    for row in csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))):
        if row.get("location.country_code") != "US" or row.get("status") in ("deprecated", "inactive"):
            continue
        url = row.get("urls.direct_download") or row.get("urls.latest") or ""
        if not url.startswith("https://"):
            continue
        dtype = row.get("data_type", "")
        ents = row.get("entity_type", "")
        auth = "none" if row.get("urls.authentication_type") in ("", "0", None) else "free_key"
        prov = row.get("provider", "").strip() or "Transit agency"
        state = row.get("location.subdivision_name", "")
        city = row.get("location.municipality", "")
        if dtype == "gtfs":
            cats, kinds, kind, what = ["travel/transit"], ["schedule"], "gtfs", "schedule (GTFS)"
        else:
            alerts = "sa" in ents
            cats = ["travel/transit_alerts"] if alerts and "tu" not in ents else ["travel/transit", "travel/transit_alerts"]
            kinds, kind, what = ["next_event", "status", "alerts"], "gtfs_rt", f"realtime (GTFS-RT {ents})"
        out.append(_rec(
            slug("mdb", row.get("mdb_source_id", ""), prov)[:110], f"{prov} {what}",
            f"{prov} transit {what} for {city or state or 'the US'}.",
            {"id": slug("transit", prov)[:60], "name": prov, "url": row.get("urls.license") or url, "authority": "official"},
            cats, kinds, "US" + (f"-{state}" if state else ""), kind, url, auth=auth,
            terms="terms_allow" if row.get("urls.license") else "unverified",
            terms_note="agency open data license" if row.get("urls.license") else "",
            cadence="realtime" if kind == "gtfs_rt" else "weekly", docs=row.get("urls.license") or url,
            entity=", ".join(x for x in (city, state) if x), origin_ref="mobilitydatabase.org",
            examples=[f"next bus {city}".strip(), f"{prov} delays"]))
    return out


# ----------------------------------------------------------------------------------- GBFS bikeshare
def gbfs() -> list[dict]:
    _, body, _ = get("https://raw.githubusercontent.com/MobilityData/gbfs/master/systems.csv", cache_hours=72, api=True)
    out = []
    for row in csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))):
        if row.get("Country Code") != "US":
            continue
        url = row.get("Auto-Discovery URL", "")
        if not url.startswith("https://"):
            continue
        nm = row.get("Name", "").strip()
        out.append(_rec(
            slug("gbfs", row.get("System ID", nm)), f"{nm} bike & scooter availability",
            f"{nm} ({row.get('Location', '')}) live bike/scooter and dock availability (GBFS).",
            {"id": slug("gbfs", nm)[:60], "name": nm, "url": row.get("URL") or url, "authority": "primary"},
            ["travel/bikeshare"], ["current_value", "map"], "US", "gbfs", url, terms="terms_allow",
            terms_note="GBFS public feed", cadence="realtime", entity=row.get("Location", ""),
            origin_ref="github.com/MobilityData/gbfs", examples=[f"bikes available {row.get('Location', '')}".strip()]))
    return out


# ----------------------------------------------------------------------------------- APIs.guru
_GURU_KEEP = {"open_data", "financial", "media", "entertainment", "location", "transport", "tools", "developer_tools",
              "iot", "text", "collaboration", "ecommerce", "social", "analytics", "machine_learning"}
_GURU_SKIP_HOSTS = ("azure.com", "googleapis.com", "amazonaws.com", "microsoft.com", "windows.net", "adyen.com",
                    "apideck.com", "twilio.com", "stripe.com", "sendgrid", "mailchimp", "atlassian", "zoom.us",
                    "box.com", "docusign", "hubspot", "salesforce", "xero", "quickbooks", "sap.com", "oracle")


def apis_guru() -> list[dict]:
    """Documented public APIs with an OpenAPI description (docs-level: the endpoint pattern needs a recipe)."""
    d = get_json("https://api.apis.guru/v2/list.json", cache_hours=168, api=True)
    out = []
    for key, api in d.items():
        ver = api["versions"].get(api.get("preferred")) or next(iter(api["versions"].values()))
        info = ver.get("info", {})
        cats = set(info.get("x-apisguru-categories", []))
        host = key.split(":")[0]
        if not (cats & _GURU_KEEP) or any(s in host for s in _GURU_SKIP_HOSTS):
            continue
        desc = _clean(info.get("description"))[:600]
        text = " ".join([info.get("title", ""), desc, " ".join(cats)])
        tcats = classify(text)
        if not tcats:
            continue  # only keep what maps onto a user need
        docs = (info.get("x-origin") or [{}])[0].get("url") or ver.get("swaggerUrl")
        if not docs or not docs.startswith("https://"):
            docs = ver.get("swaggerUrl") or ""
        if not docs.startswith("https://"):
            continue
        out.append(_rec(
            slug("apisguru", key)[:110], info.get("title", host), desc,
            {"id": slug(host)[:60], "name": info.get("x-providerName", host), "url": f"https://{host}",
             "authority": "primary"},
            tcats, ["lookup"], "global", "docs_only", "", docs=docs, terms="unverified",
            terms_note="see the provider's API terms", origin_ref="apis.guru",
            signals={"openapi": ver.get("swaggerUrl"), "categories": sorted(cats)}))
    for r in out:
        r["access"]["url_template"] = ""
    return out


# ----------------------------------------------------------------------------------- public-apis list
def public_apis() -> list[dict]:
    _, body, _ = get("https://raw.githubusercontent.com/public-apis/public-apis/master/README.md", cache_hours=168, api=True)
    out, section = [], ""
    for line in body.decode("utf-8", "replace").splitlines():
        if line.startswith("### "):
            section = line[4:].strip()
            continue
        m = re.match(r"\|\s*\[([^\]]+)\]\((https://[^)]+)\)\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|", line)
        if not m:
            continue
        name, url, desc, auth, https = (g.strip() for g in m.groups())
        text = f"{name} {desc} {section}"
        tcats = classify(text)
        if not tcats:
            continue
        a = "none" if auth.strip("` ").lower() in ("no", "") else ("oauth" if "oauth" in auth.lower() else "free_key")
        out.append(_rec(
            slug("publicapis", name)[:110], name, desc,
            {"id": slug(urlsplit(url).hostname or name)[:60], "name": name, "url": url, "authority": "primary"},
            tcats, ["lookup"], "global", "docs_only", "", auth=a, docs=url, terms="unverified",
            terms_note="see the provider's API terms", origin_ref="github.com/public-apis/public-apis",
            signals={"section": section}))
    return out


# ----------------------------------------------------------------------------------- Wikidata feeds
_WD = """SELECT ?item ?itemLabel ?feed ?site ?typeLabel WHERE {
  ?item wdt:P1019 ?feed .
  { ?item wdt:P17 wd:Q30 } UNION { ?item wdt:P495 wd:Q30 } UNION { ?item wdt:P159/wdt:P17 wd:Q30 }
  OPTIONAL { ?item wdt:P856 ?site }
  OPTIONAL { ?item wdt:P31 ?type }
  FILTER(STRSTARTS(STR(?feed), "https://"))
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} LIMIT 20000"""


def wikidata_feeds() -> list[dict]:
    """US organisations' RSS/Atom feeds from Wikidata (web feed URL, P1019)."""
    url = "https://query.wikidata.org/sparql?format=json&query=" + quote(_WD)
    d = get_json(url, cache_hours=168, api=True, headers={"Accept": "application/sparql-results+json"})
    by_feed: dict[str, dict] = {}
    for b in d["results"]["bindings"]:
        feed = b["feed"]["value"]
        nm = b.get("itemLabel", {}).get("value", "")
        if feed in by_feed or not nm or re.fullmatch(r"Q\d+", nm):
            if feed in by_feed and b.get("typeLabel"):
                by_feed[feed]["signals"]["types"].append(b["typeLabel"]["value"])
            continue
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        typ = b.get("typeLabel", {}).get("value", "")
        text = f"{nm} {typ}"
        cats = classify(text + " news feed") or ["news/feeds"]
        if "news/feeds" not in cats:
            cats.append("news/feeds")
        by_feed[feed] = _rec(
            slug("wd", qid, nm)[:110], f"{nm} feed", f"Web feed of {nm}" + (f" ({typ})" if typ else "") + ".",
            {"id": slug("wd", qid), "name": nm, "url": b.get("site", {}).get("value", feed), "authority": "primary",
             "wikidata": qid},
            cats[:3], ["latest_items"], "US", "rss", feed, terms="terms_allow",
            terms_note="public web feed for personal feed readers", cadence="daily", entity=nm,
            origin_ref=f"wikidata:{qid}", signals={"types": [typ] if typ else []})
    return list(by_feed.values())


# ----------------------------------------------------------------------------------- Census datasets
def census() -> list[dict]:
    d = get_json("https://api.census.gov/data.json", cache_hours=168, api=True)
    out = []
    for ds in d.get("dataset", []):
        if ds.get("c_isAvailable") is False:
            continue
        dist = next((x for x in ds.get("distribution", []) if x.get("format") == "API"), None)
        if not dist:
            continue
        endpoint = str(dist.get("accessURL", "")).replace("http://api.census.gov", "https://api.census.gov")
        if not endpoint.startswith("https://api.census.gov/"):
            continue
        title = ds.get("title", "")
        vint = ds.get("c_vintage", "")
        if vint and isinstance(vint, int) and vint < 2020:
            continue  # keep the current vintages; older ones stay reachable through the catalog
        text = f"{title} {ds.get('description', '')[:300]} census dataset"
        cats = classify(text) or ["science/datasets_research"]
        if "science/datasets_research" not in cats:
            cats.append("science/datasets_research")
        out.append(_rec(
            slug("census", ds.get("identifier", title).rsplit("/", 1)[-1], str(vint))[:110],
            f"{title} {vint}".strip(), _clean(ds.get("description"))[:600],
            {"id": "census", "name": "US Census Bureau", "url": "https://www.census.gov", "authority": "official"},
            cats[:3], ["lookup", "ranking", "trend"], "US", "http_json", endpoint,
            auth="free_key", terms="public_domain", terms_note="US government work; Census API key (free)",
            cadence="annual", docs=ds.get("c_documentationLink") or dist["accessURL"],
            origin_ref="api.census.gov/data.json"))
    for r in out:
        r["access"]["url_template"] = r["access"]["url_template"] + "?get={variables}&for={geography}&key={key}"
        r["access"]["params"] = [
            {"name": "variables", "kind": "dataset", "example": "NAME", "required": True},
            {"name": "geography", "kind": "us_state", "example": "state:*", "required": True},
            {"name": "key", "kind": "key", "example": None, "required": True}]
    return out


def run(name: str) -> list[dict]:
    rows = globals()[name]()
    curated = {r["id"] for r in read_jsonl(SOURCES / "curated" / "core.jsonl")}
    rows = [r for r in rows if r["id"] not in curated]
    bad = [r["id"] for r in rows if validate_record(r)]
    if bad:  # e.g. a catalog URL that embeds someone's credential: never stored
        print(f"  dropped {len(bad)} records that fail the schema: {bad[:5]}")
    rows = [r for r in rows if r["id"] not in set(bad)]
    uniq = {r["id"]: r for r in rows}
    write_jsonl(SOURCES / "harvested" / f"{name}.jsonl", list(uniq.values()))
    return list(uniq.values())
