"""
The vendors the award data covers, and the rules that turn a keyword search
into rows that are really about that vendor's network equipment. This list is
"the query": scripts/fetch_sam_awards.py and scripts/fetch_vendor_exports.py
both search for every keyword here, so adding a vendor is one entry in VENDORS
followed by a refresh (see docs/data-refresh.md).

Why rules are needed: a keyword search is broad. "palo alto" also returns the
VA Palo Alto hospital's janitorial contracts, "juniper" a Coast Guard cutter
and forestry work, "arista" about a thousand orders of a surgical hemostat
called Arista AH. Each vendor says how to tell its rows apart:

  keywords      one search per keyword; results are merged and de-duplicated
  text          regex that must match the description when require_text is set
  require_text  drop rows where the keyword only matched some other field
                (an office name, a city, the company's own name)
  require_it    drop rows that are neither IT-coded (product/service code or
                NAICS, see is_it_row) nor describe network gear in words
                (NETWORK_TERMS). The second half matters: military logistics
                contracts code everything they buy, network switches
                included, as "4220 marine lifesaving equipment"
  exclude       regexes on the description that mark a known false positive
                (the keyword used as a place, a ship, a tree, another product)
  skip_compute_only
                drop rows that mention servers/storage/HPC and no network gear;
                for a vendor whose brand covers far more than networking

Cisco keeps require_text/require_it off: the original files held every row the
"cisco" search returned and those numbers have already been presented.

Standard library only, so the refresh job image can use it as is.
Run `python scripts/vendors.py` to print the verdict on a set of sample rows.
"""
import re

VENDORS = [
    {
        "key": "cisco",
        "label": "Cisco",
        "group": "Cisco",
        "keywords": ["cisco"],
        "text": r"\bcisco\b",
        "require_text": False,
        "require_it": False,
        "exclude": [],
    },
    {
        "key": "palo_alto",
        "label": "Palo Alto Networks",
        "group": "Palo Alto Networks",
        "keywords": ["palo alto"],
        # "PALO-ALTO", "PALO- ALTO" and "PALO, ALTO" all occur.
        "text": r"\bpalo[\s\-,]*alto\b",
        "require_text": True,
        "require_it": True,
        "exclude": [
            # Palo Alto the place: the VA Palo Alto Health Care System and
            # other facilities there buy plenty of unrelated IT.
            r"\b(?:va|vamc|vahcs|vha)\s*palo\s*alto\b|\bvapahcs\b|\bpahcs\b",
            r"\bpalo\s*alto[\s,]+(?:va\b|vamc|vahcs|hcs\b|health\s*care|healthcare|medical|division|campus|county|battlefield|ca\b|california)",
            r"\b(?:at|in|for)\s+(?:the\s+)?palo\s*alto\s+(?:site|facility|location|office|building|bldg)",
        ],
    },
    {
        "key": "juniper",
        "label": "Juniper Networks",
        "group": "HPE",
        "keywords": ["juniper"],
        # No trailing boundary: "JUNIPER4400", "JUNIPERM&S" occur.
        "text": r"\bjuniper",
        "require_text": True,
        "require_it": True,
        "exclude": [
            # USCGC Juniper (a Coast Guard buoy tender) and juniper the tree.
            r"\b(?:uscgc?|cgc|cutter|wlb)\b[^.]{0,40}\bjuniper|\bjuniper\s+(?:dd|ds|dockside|dry\s*dock|wlb)\b",
            r"\bjuniper\s+(?:thinning|removal|cut|tree|trees|mastication|treatment|encroachment|woodland)|\bpinyon\b",
        ],
    },
    {
        "key": "aruba",
        "label": "Aruba Networks",
        "group": "HPE",
        "keywords": ["aruba"],
        "text": r"\baruba\b",
        "require_text": True,
        "require_it": True,
        "exclude": [],
    },
    {
        "key": "hpe",
        "label": "HPE",
        "group": "HPE",
        "keywords": ["hpe", "hewlett packard enterprise"],
        "text": r"\bhpe\b|hewlett[\s\-]*packard\s+enterprise",
        "require_text": True,
        "require_it": True,
        # Most HPE-branded federal spend is servers, storage and
        # supercomputers, not network gear. Set this True to drop rows that
        # are clearly compute-only (about 450 transactions as of Nov 2026).
        # Left off by request: better to carry extra HPE rows than miss any.
        "skip_compute_only": False,
        "exclude": [
            r"health\s+program\s+evaluation",
        ],
    },
    {
        "key": "arista",
        "label": "Arista Networks",
        "group": "Arista Networks",
        "keywords": ["arista"],
        "text": r"\barista\b",
        "require_text": True,
        "require_it": True,
        "exclude": [
            # "Arista AH" is an absorbable surgical hemostat (about a
            # thousand military medical orders), unrelated to Arista Networks.
            r"\barista\s+ah\b|\bhmst\b|hemostat|bionert",
        ],
    },
    {
        "key": "fortinet",
        "label": "Fortinet",
        "group": "Fortinet",
        # Orders often name the product line (FortiGate firewalls) and not
        # the company, so search for both.
        "keywords": ["fortinet", "fortigate"],
        "text": r"\bforti(?:net|gate|switch|analyzer|manager|ap\b|client|web|mail|siem|nac|sandbox|authenticator|token|care|guard|os\b)",
        "require_text": True,
        # The name is unambiguous (not a place, ship or other product), so
        # every row that names it is kept, whatever its product code.
        "require_it": False,
        "exclude": [],
    },
]

VENDOR_BY_KEY = {v["key"]: v for v in VENDORS}

# Product/service code prefixes that mean IT or communications equipment and
# services: 7x IT hardware/software, Dx IT and telecom services, 58
# communication equipment, 59 electrical/electronic components, 60 fiber
# optics.
_IT_PSC_PREFIXES_1 = ("7", "D")
_IT_PSC_PREFIXES_2 = ("58", "59", "60")

# NAICS codes for computer/network manufacturing, wholesale, software,
# telecom and IT services.
_IT_NAICS_PREFIXES = (
    "3341",    # computer and peripheral equipment manufacturing
    "3342",    # communications equipment manufacturing
    "3344",    # semiconductor and electronic components
    "42343",   # computer and software wholesalers (NOT all of 4234: 423450
               # is medical-equipment wholesalers, which let the surgical
               # hemostat orders through)
    "42369",   # other electronic parts and equipment wholesalers
    "5112", "5132",  # software publishers (2017 / 2022 codes)
    "517",     # telecommunications
    "518",     # data processing and hosting
    "5415",    # computer systems design and related services
)

# Words that mean network/security gear or its licensing. A row that names
# the vendor and one of these is kept even when its product code is not an
# IT code. Deliberately excludes generic words ("hardware", "maintenance",
# "support") that also describe door hardware and janitorial support at a
# place called Palo Alto.
NETWORK_TERMS = re.compile(
    r"\b(?:firewalls?|ngfw|switch(?:es|ing)?|routers?|routing|network(?:s|ing)?|wireless|wi-?fi|wlan"
    r"|access\s+points?|aps?\b|licen[sc](?:e|es|ing)|subscriptions?|software|appliances?|transceivers?|sfp\+?"
    r"|sd-?\s?wan|vpn"
    r"|panorama|prisma|cortex|global\s*protect|wildfire|pa-\d{3,4}"
    r"|junos|mist|srx\s?\d+|qfx\s?\d+|ex\s?\d{4}|mx\s?\d{2,4}"
    r"|clearpass|airwave|aruba\s+central|cx\s?\d{4,5}|proliant|synergy|nimble|simplivity|greenlake)\b",
    re.I,
)

# Narrower than NETWORK_TERMS: actual network equipment, no licensing words.
NETWORK_GEAR = re.compile(
    r"\b(?:aruba|juniper|switch(?:es|ing)?|routers?|wireless|wi-?fi|wlan|network(?:s|ing)?|firewalls?"
    r"|access\s+points?|clearpass|sd-?\s?wan|transceivers?|flexnetwork|flexfabric|comware)\b",
    re.I,
)

# Servers, storage and high-performance computing.
COMPUTE_TERMS = re.compile(
    r"\b(?:servers?|proliant|blades?|storage|san\b|nas\b|tape|supercomput\w*|hpc|high[\s\-]performance\s+comput\w*"
    r"|cray|apollo|synergy|superdome|edgeline|el8000|nimble|3par|alletra|primera|msa\b|storeonce|simplivity"
    r"|greenlake|ezmeral|nonstop|compute|hyperconverged|nutanix|vmware)\b",
    re.I,
)

_TEXT = {v["key"]: re.compile(v["text"], re.I) for v in VENDORS}
_EXCLUDE = {v["key"]: [re.compile(p, re.I) for p in v.get("exclude", [])] for v in VENDORS}

DROP_NO_KEYWORD = "keyword not in description"
DROP_FALSE_POSITIVE = "known false-positive phrase"
DROP_NOT_NETWORK = "not IT-coded and no network terms in description"
DROP_COMPUTE_ONLY = "server/storage/HPC with no network gear mentioned"


def all_keywords() -> list:
    """Every search keyword, in vendor order: the default query."""
    return [kw for v in VENDORS for kw in v["keywords"]]


def vendor_for_keyword(keyword: str):
    """The vendor a search keyword belongs to, or None for an ad-hoc term."""
    wanted = (keyword or "").strip().lower()
    for v in VENDORS:
        if wanted in (k.lower() for k in v["keywords"]):
            return v
    return None


def is_it_row(psc_code, naics_code) -> bool:
    psc = str(psc_code or "").strip().upper()
    naics = str(naics_code or "").strip().split(".")[0]
    if psc[:1] in _IT_PSC_PREFIXES_1 or psc[:2] in _IT_PSC_PREFIXES_2:
        return True
    return naics.startswith(_IT_NAICS_PREFIXES)


def keep_row(vendor: dict, description: str, psc_code="", naics_code="") -> tuple:
    """(keep?, reason dropped). `description` is every description field of
    the row joined with a space."""
    text = description or ""
    key = vendor["key"]
    if vendor["require_text"] and not _TEXT[key].search(text):
        return False, DROP_NO_KEYWORD
    if any(rx.search(text) for rx in _EXCLUDE[key]):
        return False, DROP_FALSE_POSITIVE
    if vendor["require_it"] and not is_it_row(psc_code, naics_code) and not NETWORK_TERMS.search(text):
        return False, DROP_NOT_NETWORK
    if vendor.get("skip_compute_only") and COMPUTE_TERMS.search(text) and not NETWORK_GEAR.search(text):
        return False, DROP_COMPUTE_ONLY
    return True, ""


def tag_vendors(description: str, psc_code="", naics_code="") -> list:
    """Vendor keys for a row that is already in the data, in VENDORS order.
    Used by db/build_db.py to stamp each record for the app's vendor filter.

    A vendor other than the first claims the row when its own rules would
    have kept it. The first vendor (Cisco) claims the row when its name is in
    the description, and also any row nobody else claims: the data began as
    Cisco's unfiltered keyword results, some of which never spell the name
    out. An order naming two vendors carries both."""
    text = description or ""
    first, others = VENDORS[0], VENDORS[1:]
    tags = [v["key"] for v in others
            if _TEXT[v["key"]].search(text) and keep_row(v, text, psc_code, naics_code)[0]]
    if _TEXT[first["key"]].search(text) or not tags:
        tags.insert(0, first["key"])
    return tags


# Real descriptions seen in the data, with the verdict each must get. Run this
# file to check them after editing a rule.
_SAMPLES = [
    ("cisco", "CISCO SMARTNET MAINTENANCE", "7A21", "541519", True),
    ("cisco", "TO INDUCT AIRCRAFT FOR DEPOT MAINTENANCE", "J015", "488190", True),  # cisco keeps everything
    ("palo_alto", "PALO ALTO NETWORKS ENTERPRISE LICENSE AGREEMENT (ELA)", "7G21", "541519", True),
    ("palo_alto", "PALO-ALTO VIRTUAL MACHINES FIREWALL CREDITS", "7G21", "541519", True),
    ("palo_alto", "4569641644!PALO ALTO NETWORKS PA-3440 WITH REDUNDAN", "4220", "423990", True),
    ("palo_alto", "JANITORIAL SERVICES AT VA PALO ALTO HEALTH CARE SYSTEM (VAPAHCS)", "S201", "561720", False),
    ("palo_alto", "MODALITY: XR MAMMO STATION: VAMC PALO ALTO, CA REQUIREMENT: ADD SOFTWARE UPGRADE", "6525", "334517", False),
    ("palo_alto", "PURCHASE OF RECLINERS FOR THE PALO ALTO VA HEALTH CARE SYSTEM", "7110", "337127", False),
    ("palo_alto", "DESIGN BUILD AT PALO ALTO FOR BUILDING 51", "Y1AZ", "236220", False),
    ("juniper", "FY24 CORE ROUTER REFRESH JUNIPER", "7B22", "541519", True),
    ("juniper", "TITLE: FY25, STVP, V3GATE, JUNIPER4400", "7B22", "541519", True),
    ("juniper", "JUNIPER DD FY26", "J999", "336611", False),
    ("juniper", "APACHE COUNTY JUNIPER THINNING", "F018", "115310", False),
    ("juniper", "SCUTTLE QA BA 3-D ST", "5820", "332999", False),  # company named Juniper, not the product
    ("aruba", "4567609866!HPE ARUBA NETWORKING CX 10000 48P BUNDLE", "4220", "423990", True),
    ("hpe", "HPE PROLIANT DL20 GEN II", "7C20", "334111", True),
    ("hpe", "HEALTH PROGRAM EVALUATION (HPE)", "R499", "541611", False),
    ("hpe", "4562722360!BOTTLE LARGE WM HPE 1/2GAL 2L", "6640", "423450", False),
    ("arista", "ARISTA NETWORKING SWITCHES", "7G21", "541519", True),
    ("arista", "4567700313!HMST ABS BIONERT ARISTA AH MPH PLNT STRH", "6515", "423450", False),
    ("arista", "TASK ORDER FOR BASE PORTSMOUTH, C5IC, ARISTA WASHINGTON JANITORIAL SERVICES.", "S201", "561720", False),
    ("fortinet", "FORTINET FORTIGATE BX ANNUAL LICENSE-YR4", "7A21", "541519", True),
    ("fortinet", "4564762709!FORTINET FEDERAL  COTERM", "4220", "423990", True),
]


if __name__ == "__main__":
    failed = 0
    for key, desc, psc, naics, expected in _SAMPLES:
        keep, reason = keep_row(VENDOR_BY_KEY[key], desc, psc, naics)
        ok = keep == expected
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {key:10s} {'keep' if keep else 'drop'}  {desc[:70]}" + (f"   [{reason}]" if reason else ""))
    for desc, psc, naics, expected in [
        ("CISCO SMARTNET MAINTENANCE", "7A21", "541519", ["cisco"]),
        ("TO INDUCT AIRCRAFT FOR DEPOT MAINTENANCE", "J015", "488190", ["cisco"]),  # unclaimed -> first vendor
        ("PALO ALTO NETWORKS PA-3440 FIREWALL", "7G21", "541519", ["palo_alto"]),
        ("CISCO 9500, PALO ALTO PA-3420, AND HW ACCESSORIES", "7G21", "541519", ["cisco", "palo_alto"]),
        ("NETWORKING EQUIPMENT ARUBA, JUNIPER DELL", "7K20", "541519", ["juniper", "aruba"]),
        ("CISCO PHONES FOR VA PALO ALTO HEALTH CARE SYSTEM", "7G21", "541519", ["cisco"]),  # the place, not the vendor
    ]:
        got = tag_vendors(desc, psc, naics)
        ok = got == expected
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} tags {got}  {desc[:60]}")
    print(f"\n{len(_SAMPLES) + 6 - failed} of {len(_SAMPLES) + 6} samples as expected; query keywords: {', '.join(all_keywords())}")
    raise SystemExit(1 if failed else 0)
