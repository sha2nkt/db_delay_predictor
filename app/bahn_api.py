from curl_cffi import requests

# bahn.de sits behind Akamai Bot Manager, which fingerprints the TLS/HTTP2
# client (not just cookies) and returns 403 OPS_BLOCKED to plain HTTP stacks
# like httpx/requests. curl_cffi's impersonate="chrome" reproduces a real
# Chrome ClientHello, which passes the check with no cookie warmup needed.
BASE_URL = "https://www.bahn.de/web/api"

client = requests.AsyncSession(
    impersonate="chrome",
    timeout=20,
    headers={"Accept": "application/json"},
)

ALL_PRODUCTS = ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"]


async def locations(query: str) -> list[dict]:
    resp = await client.get(f"{BASE_URL}/reiseloesung/orte", params={"suchbegriff": query, "typ": "ALL", "limit": 8})
    resp.raise_for_status()
    return resp.json()


async def journeys(from_id: str, to_id: str, departure_iso: str, paging_ref: str | None = None) -> dict:
    """from_id/to_id are full HAFAS location ids (A=1@O=...@L=...@) from locations().

    paging_ref is a verbindungReference.earlier/later token from a previous response;
    when set, the API returns the adjacent result page instead of the requested time.
    """
    body = {
        "abfahrtsHalt": from_id,
        "ankunftsHalt": to_id,
        "anfrageZeitpunkt": departure_iso,
        "ankunftSuche": "ABFAHRT",
        "klasse": "KLASSE_2",
        "produktgattungen": ALL_PRODUCTS,
        "reisende": [{
            "typ": "ERWACHSENER",
            "ermaessigungen": [{"art": "KEINE_ERMAESSIGUNG", "klasse": "KLASSENLOS"}],
            "alter": [],
            "anzahl": 1,
        }],
        "schnelleVerbindungen": True,
        "sitzplatzOnly": False,
        "bikeCarriage": False,
        "reservierungsKontingenteVorhanden": False,
    }
    if paging_ref:
        body["pagingReference"] = paging_ref
    resp = await client.post(f"{BASE_URL}/angebote/fahrplan", json=body)
    resp.raise_for_status()
    return resp.json()
