"""US-only location check, shared by discovery, scoring, the free pre-filter and the apply queue.

    non_us_location("Remote, QC, CAN")        -> "QC"        (not a US job)
    non_us_location("Remote, USA")            -> None        (fine)
    non_us_location("Remote, US or Canada")   -> None        (open to US applicants)

USA and remote-USA roles only (turn off with us_only: false in config/settings.json). A location is rejected when it
names a non-US place AND gives no sign it is also open in the US. Empty or plain "Remote" locations pass
(the apply agent checks the posting itself).

Source of truth: this file. A copy lives in ~/.applypilot-venv/.../applypilot/us_only.py for the discovery and
scoring code (see local-patches/README.md); keep them identical.
"""
import re

# case-sensitive 2-letter Canadian province codes after a comma, e.g. "Toronto, ON" / "Remote, QC, CAN"
_PROVINCE_CODE = re.compile(r",\s*(?:ON|BC|AB|QC|NS|NB|MB|SK|PE|NL)\s*(?:,|$|\(|\)|-|/)")
_CAN_SUFFIX = re.compile(r",\s*CAN\b")

_NON_US = re.compile(
    r"\bcanada\b|\bontario\b|british columbia|\balberta\b|qu[eé]bec|nova scotia|new brunswick|manitoba|saskatchewan|"
    r"newfoundland|toronto|montr[eé]al|calgary|edmonton|mississauga|winnipeg|teletravail|"
    # India
    r"\bindia\b|bengaluru|bangalore|hyderabad|\bpune\b|mumbai|chennai|gurgaon|gurugram|noida|kolkata|ahmedabad|"
    r"new delhi|\bdelhi\b(?!,\s*NY)|kochi|coimbatore|\bindore\b|"
    # UK
    r"united kingdom|,\s*uk\b|\buk\b|\bu\.k\.|\bgbr\b|england|scotland|\bwales\b|northern ireland|edinburgh|cardiff|belfast|"
    r"\blondon\b(?!,\s*(?:KY|OH|WV|AR|TX|WI|CA|MN))|"
    # other countries / regions (the user wants the US only)
    r"philippines|(?<!new )\bmexico\b|brazil|argentina|colombia|\bchile\b|germany|france|spain|italy|netherlands|poland|"
    r"portugal|romania|ukraine|ireland|sweden|norway|denmark|finland|switzerland|austria|belgium|czech|hungary|israel|"
    r"turkey|united arab emirates|\buae\b|dubai|saudi|egypt|nigeria|kenya|south africa|pakistan|bangladesh|sri lanka|"
    r"singapore|malaysia|indonesia|thailand|vietnam|japan|\bkorea\b|china|hong kong|taiwan|australia|new zealand|"
    r"nepal|bhutan|myanmar|cambodia|\blaos\b|mongolia|macau|kazakhstan|uzbekistan|armenia|azerbaijan|qatar|kuwait|bahrain|"
    r"\boman\b|lebanon|morocco|tunisia|algeria|ghana|ethiopia|uganda|tanzania|rwanda|senegal|cameroon|zimbabwe|zambia|"
    r"\bperu\b|ecuador|bolivia|paraguay|uruguay|venezuela|costa rica|guatemala|honduras|el salvador|nicaragua|panama|"
    r"dominican republic|jamaica|trinidad|greece|croatia|serbia|bulgaria|slovakia|slovenia|estonia|latvia|lithuania|"
    r"luxembourg|iceland|cyprus|\bmalta\b|bosnia|albania|moldova|belarus|\brussia\b|"
    r"\bemea\b|\bapac\b|\blatam\b",
    re.I,
)

_US_MARK = re.compile(
    r"united states|\busa\b|\bu\.s\.a?\b|,\s*US\b|"
    r",\s*(?:AL|AK|AZ|AR|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b",
)


def non_us_location(location):
    """The text that marks this location as outside the US, or None if it is fine / unknown."""
    if not location:
        return None
    loc = str(location)
    m = _PROVINCE_CODE.search(loc) or _CAN_SUFFIX.search(loc) or _NON_US.search(loc)
    if not m:
        return None
    # "Remote, United States or Canada" and similar multi-country postings are open to US applicants
    if _US_MARK.search(loc) or re.search(r"\bunited states\b|\bUSA\b", loc, re.I):
        return None
    return m.group(0).strip(" ,()/-") or m.group(0)


if __name__ == "__main__":
    cases = {
        "REMOTE/TELETRAVAIL, QC, CAN": True, "Toronto, ON": True, "Remote, Canada": True, "Remote, USA": False,
        "Remote, United States or Canada": False, "London, United Kingdom": True, "London, KY": False,
        "New York, NY, USA": False, "Austin, TX (On-site)": False, "New York, On site": False,
        "Bengaluru, Karnataka, India": True, "Albuquerque, New Mexico": False, "Mexico City, Mexico": True,
        "Vancouver, WA": False, "Vancouver, BC, Canada": True, "Remote": False, "": False, "Dublin, Ireland": True,
        "Dublin, CA": False, "Edinburgh, Scotland, UK": True, "Remote, EMEA": True, "Ottawa, IL": False,
        "San Francisco, CA": False, "Toronto, ON, CA": True, "Remote - US": False, "Pune, Maharashtra": True,
        "Lalitpur, Bāgmatī, Nepal": True, "Lima, Peru": True, "Lebanon, PA": False, "Peru, IN": False, "Doha, Qatar": True,
    }
    bad = [(k, v) for k, v in cases.items() if bool(non_us_location(k)) != v]
    print("all", len(cases), "cases pass" if not bad else f"FAILED: {bad}")
