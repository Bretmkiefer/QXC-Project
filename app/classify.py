"""
Text-based tagging of purchase records: Cisco model numbers, equipment type,
and purchase category (hardware vs. maintenance/support vs. software).

Everything here is a best-effort heuristic over free-text descriptions and the
product/service code, because the underlying data has no structured SKU or
line-item fields. Each tagger is conservative: it prefers "Unspecified" /
"Other" over guessing, so a filter on a tag returns records that really do
match, at the cost of missing some that don't spell it out.
"""
import re

# --------------------------------------------------------------- models --
# Each alternative must contain a digit sequence and, where a bare number
# would be ambiguous (phones, small-business lines, Meraki), a hyphen or a
# family word next to it.
_MODEL_ALTERNATIVES = [
    # Catalyst switches / Catalyst 8000 routers, by family word or bare SKU
    r"(?:Catalyst|Cat)\s?[0-9]{4}[A-Z0-9\-]*",
    r"C9[0-9]{3}[A-Z0-9\-]*",
    r"C8[0-9]{3}[A-Z0-9\-]*",
    r"C1[0-9]{3}-[A-Z0-9\-]+",
    r"WS-[A-Z]-?[0-9]{4}[A-Z0-9\-]*",
    r"CBS[0-9]{3}[A-Z]?-[A-Z0-9\-]+",
    r"(?:SG|SF)[0-9]{3}[A-Z]?-[A-Z0-9\-]+",
    # Nexus / MDS
    r"Nexus\s?[0-9]{3,5}[A-Z0-9\-]*",
    r"Nexus\s?[3579]K[A-Z0-9\-]*",
    r"N[3579]K-[A-Z0-9\-]+",
    r"DS-C[0-9]{4}[A-Z0-9\-]*",
    r"MDS\s?9[0-9]{3}[A-Z0-9\-]*",
    # Routers
    r"ISR\s?[0-9]{4}[A-Z0-9\-/]*",
    r"ASR\s?[0-9]{3,4}[A-Z0-9\-]*",
    r"NCS[\s\-]?[0-9]{3,4}[A-Z0-9\-]*",
    # Security
    r"(?:Firepower|FPR)[\s\-]?[0-9]{3,4}[A-Z0-9\-]*",
    r"ASA[\s\-]?5[0-9]{3}[A-Z0-9\-]*",
    # Compute
    r"UCS[A-Z]?-[A-Z0-9\-]+",
    r"UCS\s?[0-9]{4}[A-Z0-9\-]*",
    r"HX[0-9]{3}[A-Z]?-[A-Z0-9\-]+",
    r"C2[24]0[\s\-]?M[0-9][A-Z0-9\-]*",
    # Meraki
    r"Meraki\s?[A-Z]{2}[0-9]{2,3}[A-Z0-9\-]*",
    r"M[XRSVTG][0-9]{2,3}-[A-Z0-9\-]+",
    # Wireless
    r"AIR-[A-Z0-9\-]+",
    r"Aironet\s?[0-9]{3,4}[A-Z0-9\-]*",
    r"98[0-9]{2}-[A-Z0-9\-]+",
    # Collaboration / phones
    r"CP-?[0-9]{4}[A-Z0-9\-]*",
    r"(?:IP\s?)?Phone\s?(?:[67]8[0-9]{2}|88[0-9]{2}|99[0-9]{2})[A-Z0-9\-]*",
    r"Webex\s?(?:Room\s?(?:Kit|Bar|Pro)|Board|Desk)(?:\s?(?:Pro|Plus|Mini))?",
    # Optics / modules (hyphen required)
    r"(?:Q?SFP\+?|GLC|XFP|CFP)[A-Z0-9\+]*-[A-Z0-9\-]+",
]
MODEL_PATTERN = re.compile(r"\b(" + "|".join(_MODEL_ALTERNATIVES) + r")(?![A-Za-z0-9])", re.IGNORECASE)

# Bare phone numbers (8841, 7841, ...) only count when the text is clearly
# about phones, otherwise four-digit numbers are far too ambiguous.
_PHONE_CONTEXT = re.compile(r"\b(?:phones?|voip|handsets?)\b", re.IGNORECASE)
_BARE_PHONE = re.compile(r"\b((?:[67]8[0-9]{2}|88[0-9]{2}|99[0-9]{2}))(?:-[A-Z0-9]+)?\b")


def extract_models(text) -> list:
    if not text or (isinstance(text, float) and text != text):
        return []
    text = str(text)
    seen = []
    for m in MODEL_PATTERN.findall(text):
        norm = re.sub(r"\s+", " ", m).strip().upper()
        if norm not in seen:
            seen.append(norm)
    if _PHONE_CONTEXT.search(text):
        for m in _BARE_PHONE.findall(text):
            norm = f"PHONE {m}"
            if norm not in seen and not any(m in s for s in seen):
                seen.append(norm)
    return seen


# ------------------------------------------------------------ equipment --
# Checked per record; a record can carry several types. Order only affects
# display order. Patterns run over the description and the extracted models.
# Catalyst 91xx/98xx are wireless APs/controllers and Catalyst 8xxx are
# routers, so the generic word "catalyst" only counts as a switch otherwise.
EQUIPMENT_RULES = [
    ("Router", re.compile(
        r"\brouters?\b|\bISR\s?[0-9]{4}|\bASR\s?[0-9]|\bNCS[\s\-]?[0-9]|\bC8[0-9]{3}|\bC1[0-9]{3}-|"
        r"integrated services|\bsd-?wan\b|\bcatalyst\s?8[0-9]{3}", re.I)),
    ("Switch", re.compile(
        r"\bswitch(?:es|ing)?\b|\bcatalyst\b(?!\s?(?:91|98|8[0-9]{3}))|\bnexus\b|\bC9[2-6][0-9]{2}|\bWS-C|\bCBS[0-9]{3}|"
        r"\b(?:SG|SF)[0-9]{3}|\bN[3579]K\b|\bMDS\b|\bDS-C[0-9]", re.I)),
    ("Wireless", re.compile(
        r"\bwireless\b|\bwi-?fi\b|\baccess points?\b|\baironet\b|\bAIR-|\bWLC\b|\bC91[0-9]{2}|\bC98[0-9]{2}|"
        r"\bcatalyst\s?(?:91|98)[0-9]{2}|\bMR[0-9]{2}", re.I)),
    ("Firewall / Security", re.compile(
        r"\bfirewalls?\b|\bfirepower\b|\bFPR[\s\-]?[0-9]|\bASA[\s\-]?5|\bidentity services\b|\bISE\b|\bumbrella\b|"
        r"\bduo\b|\bstealthwatch\b|\bsecure (?:endpoint|firewall|email|web|network)\b|\bIPS\b|\bFTD\b", re.I)),
    ("Voice / Collaboration", re.compile(
        r"\bphones?\b|\bvoip\b|\bwebex\b|\bvideo\b|\bcodec\b|\broom kit\b|\bdesk (?:pro|mini)\b|\bboard pro\b|"
        r"\btelepresence\b|\bcall ?manager\b|\bcucm\b|\bunified communications?\b|\bjabber\b|\bheadsets?\b|"
        r"\bconferenc|\bCP-?[0-9]{4}", re.I)),
    ("Server / Compute", re.compile(
        r"\bUCS\b|\bUCS[A-Z]?-|\bservers?\b|\bblades?\b|\bhyperflex\b|\bHX[0-9]{3}|\bC2[24]0[\s\-]?M[0-9]", re.I)),
    ("Optics / Modules", re.compile(
        r"\bsfps?\b|\bqsfp\b|\btransceivers?\b|\boptics?\b|\bGLC-|\bnetwork modules?\b|\bline ?cards?\b|\bmodules?\b", re.I)),
]


EQUIPMENT_TYPES = [name for name, _ in EQUIPMENT_RULES] + ["Unspecified"]


def classify_equipment(description, models) -> list:
    hay = f"{description or ''} {' '.join(models or [])}"
    found = [name for name, rx in EQUIPMENT_RULES if rx.search(hay)]
    return found or ["Unspecified"]


# ------------------------------------------------------------- category --
HARDWARE, SUPPORT, SOFTWARE, OTHER = "Hardware", "Maintenance & Support", "Software & Licensing", "Other"
CATEGORIES = [HARDWARE, SUPPORT, SOFTWARE, OTHER]

# Strong signals only. "support" or "maintenance" alone is not enough when the
# record is clearly about buying a physical thing for a maintenance *facility*.
_SUPPORT_TEXT = re.compile(
    r"smart ?net|\bcx\b|\btac\b|\bwarranty\b|\bsustainment\b|\bmaintenance\b(?!\s+(?:shop|facility|building|hangar|depot))|"
    r"\b(?:technical|software|premium|product|hardware|network|enterprise|annual|extended|advanced)\s+support\b|"
    r"\bsupport\s+(?:services?|contracts?|agreements?|plans?|renewals?|only)\b|\bservices?\s+support\b|"
    r"\badvanced services\b|\brenewal\b[^.]{0,40}\bsupport\b|\bservice\s+(?:contracts?|agreements?|plans?)\b|"
    r"\brenewal\s+of\s+(?:support|maintenance|smartnet)|\b(?:support|maintenance)\s+renewal|\bsupport\s+(?:and|&)\s+maintenance|"
    r"\bmaintenance\s+(?:and|&)\s+support|\bbreak[\s\-]?fix\b|\bmanaged services?\b|"
    r"\bsupport\b.*\b(?:12|24|36|60)\s?(?:months?|mos?)\b",
    re.I)
_SOFTWARE_TEXT = re.compile(
    r"\blicen[sc]es?\b|\blicensing\b|\bsubscriptions?\b|\bsoftware\b|\benterprise agreement\b|\bsaas\b|\bxaas\b|"
    r"\bduo\b|\bumbrella\b|\bDNA\b|\banyconnect\b|\bcloud\b|\bISE\b", re.I)
_HARDWARE_TEXT = re.compile(
    r"\bswitch(?:es)?\b|\brouters?\b|\bcatalyst\b|\bnexus\b|\bfirewalls?\b|\bphones?\b|\bservers?\b|\bUCS\b|"
    r"\baccess points?\b|\bwireless\b|\bmodules?\b|\btransceivers?\b|\bSFP\b|\bcables?\b|\bhardware\b|\bequipment\b|"
    r"\bappliances?\b|\bchassis\b|\bline ?cards?\b|\bASA\b|\bfirepower\b|\bISR\b|\bASR\b|\bC[0-9]{4}|\bWS-C|"
    r"\bnetwork(?:ing)? (?:gear|devices?|products?|components?)\b|\bcodec\b|\bheadsets?\b|\bpower (?:supply|cord)\b|"
    r"\bantenna\b|\brack\b|\bperipherals?\b|\broom kit\b|\bdesk (?:pro|mini)\b|\bboard pro\b|\bstack\b|\bsfps?\b|"
    r"\bfpr[0-9]|\bwebex (?:room|board|desk)\b|\bHW\b|\bhand ?sets?\b|\bblades?\b", re.I)
_BUYING_WORDS = re.compile(r"\brenew|\bsubscription|\blicen[sc]es?\b", re.I)


def classify_category(description, psc_code, psc_description) -> list:
    """Return every category a purchase belongs to (a bundle such as
    "switches and SmartNet" is both Hardware and Maintenance & Support).
    The description decides when it says anything; the product/service code
    is only the fallback when the description is silent or generic."""
    desc = description or ""
    code = (psc_code or "").strip().upper()
    pdesc = (psc_description or "").upper()

    labels = set()
    if _SUPPORT_TEXT.search(desc):
        labels.add(SUPPORT)
    if _SOFTWARE_TEXT.search(desc):
        labels.add(SOFTWARE)
    if _HARDWARE_TEXT.search(desc):
        labels.add(HARDWARE)
    if labels:
        # A box named alongside a licence word is a hardware purchase that
        # happens to mention a licence; keep Software only when the text is
        # explicitly about renewing or subscribing to it.
        if SOFTWARE in labels and HARDWARE in labels and not _BUYING_WORDS.search(desc):
            labels.discard(SOFTWARE)
        return [c for c in CATEGORIES if c in labels]

    if code.startswith("J") or code == "D319" or "MAINTENANCE" in pdesc or "SUPPORT SERVICES" in pdesc:
        return [SUPPORT]
    if "AS A SERVICE" in pdesc or ("SOFTWARE" in pdesc and "HARDWARE" not in pdesc and "HW" not in pdesc):
        return [SOFTWARE]
    if "HARDWARE" in pdesc or "(HW" in pdesc or re.fullmatch(r"[0-9]{4}", code):
        return [HARDWARE]
    return [OTHER]
