import httpx

client = httpx.AsyncClient(
    base_url="https://www.bahn.de/web/api",
    timeout=20,
    headers={"Accept": "application/json"},
)

ALL_PRODUCTS = ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"]


async def locations(query: str) -> list[dict]:
    resp = await client.get("/reiseloesung/orte", params={"suchbegriff": query, "typ": "ALL", "limit": 8})
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
    resp = await client.post("/angebote/fahrplan", json=body)
    resp.raise_for_status()
    return resp.json()
