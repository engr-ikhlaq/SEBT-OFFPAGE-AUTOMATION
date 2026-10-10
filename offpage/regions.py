"""Google search targeting: which country's results (the `gl` parameter)
and which interface language (`hl`) a scrape uses - independently
selectable, same as Google's own Search Settings exposes them as two
separate choices rather than one bundled list. See Compose's "Result
region" / "Language" dropdowns, app.py's /scrape/start, and scrape_job.py.

Countries are ISO 3166-1 (the standard `gl` follows), with Google's one
long-standing quirk: it uses "uk" rather than ISO's "gb" for the United
Kingdom. Rather than keep a table of country-specific Google domains
(google.co.uk, google.de, ...) - most of which now just redirect to
google.com anyway - every region is reached through google.com itself
with `gl`/`hl` query parameters, which is the mechanism Google documents
and supports for all of them uniformly.
"""
from __future__ import annotations

COUNTRIES: dict[str, str] = {
    "af": "Afghanistan", "al": "Albania", "dz": "Algeria", "as": "American Samoa",
    "ad": "Andorra", "ao": "Angola", "ai": "Anguilla", "aq": "Antarctica",
    "ag": "Antigua and Barbuda", "ar": "Argentina", "am": "Armenia", "aw": "Aruba",
    "au": "Australia", "at": "Austria", "az": "Azerbaijan", "bs": "Bahamas",
    "bh": "Bahrain", "bd": "Bangladesh", "bb": "Barbados", "by": "Belarus",
    "be": "Belgium", "bz": "Belize", "bj": "Benin", "bm": "Bermuda",
    "bt": "Bhutan", "bo": "Bolivia", "ba": "Bosnia and Herzegovina", "bw": "Botswana",
    "br": "Brazil", "io": "British Indian Ocean Territory", "bn": "Brunei",
    "bg": "Bulgaria", "bf": "Burkina Faso", "bi": "Burundi", "kh": "Cambodia",
    "cm": "Cameroon", "ca": "Canada", "cv": "Cape Verde", "ky": "Cayman Islands",
    "cf": "Central African Republic", "td": "Chad", "cl": "Chile", "cn": "China",
    "cx": "Christmas Island", "cc": "Cocos Islands", "co": "Colombia", "km": "Comoros",
    "cg": "Congo", "cd": "Congo (DRC)", "ck": "Cook Islands", "cr": "Costa Rica",
    "ci": "Côte d'Ivoire", "hr": "Croatia", "cu": "Cuba", "cw": "Curaçao",
    "cy": "Cyprus", "cz": "Czechia", "dk": "Denmark", "dj": "Djibouti",
    "dm": "Dominica", "do": "Dominican Republic", "ec": "Ecuador", "eg": "Egypt",
    "sv": "El Salvador", "gq": "Equatorial Guinea", "er": "Eritrea", "ee": "Estonia",
    "sz": "Eswatini", "et": "Ethiopia", "fk": "Falkland Islands", "fo": "Faroe Islands",
    "fj": "Fiji", "fi": "Finland", "fr": "France", "gf": "French Guiana",
    "pf": "French Polynesia", "ga": "Gabon", "gm": "Gambia", "ge": "Georgia",
    "de": "Germany", "gh": "Ghana", "gi": "Gibraltar", "gr": "Greece",
    "gl": "Greenland", "gd": "Grenada", "gp": "Guadeloupe", "gu": "Guam",
    "gt": "Guatemala", "gg": "Guernsey", "gn": "Guinea", "gw": "Guinea-Bissau",
    "gy": "Guyana", "ht": "Haiti", "hn": "Honduras", "hk": "Hong Kong",
    "hu": "Hungary", "is": "Iceland", "in": "India", "id": "Indonesia",
    "ir": "Iran", "iq": "Iraq", "ie": "Ireland", "im": "Isle of Man",
    "il": "Israel", "it": "Italy", "jm": "Jamaica", "jp": "Japan",
    "je": "Jersey", "jo": "Jordan", "kz": "Kazakhstan", "ke": "Kenya",
    "ki": "Kiribati", "kw": "Kuwait", "kg": "Kyrgyzstan", "la": "Laos",
    "lv": "Latvia", "lb": "Lebanon", "ls": "Lesotho", "lr": "Liberia",
    "ly": "Libya", "li": "Liechtenstein", "lt": "Lithuania", "lu": "Luxembourg",
    "mo": "Macao", "mg": "Madagascar", "mw": "Malawi", "my": "Malaysia",
    "mv": "Maldives", "ml": "Mali", "mt": "Malta", "mh": "Marshall Islands",
    "mq": "Martinique", "mr": "Mauritania", "mu": "Mauritius", "yt": "Mayotte",
    "mx": "Mexico", "fm": "Micronesia", "md": "Moldova", "mc": "Monaco",
    "mn": "Mongolia", "me": "Montenegro", "ms": "Montserrat", "ma": "Morocco",
    "mz": "Mozambique", "mm": "Myanmar", "na": "Namibia", "nr": "Nauru",
    "np": "Nepal", "nl": "Netherlands", "nc": "New Caledonia", "nz": "New Zealand",
    "ni": "Nicaragua", "ne": "Niger", "ng": "Nigeria", "nu": "Niue",
    "nf": "Norfolk Island", "kp": "North Korea", "mk": "North Macedonia",
    "no": "Norway", "om": "Oman", "pk": "Pakistan", "pw": "Palau",
    "ps": "Palestine", "pa": "Panama", "pg": "Papua New Guinea", "py": "Paraguay",
    "pe": "Peru", "ph": "Philippines", "pn": "Pitcairn Islands", "pl": "Poland",
    "pt": "Portugal", "pr": "Puerto Rico", "qa": "Qatar", "re": "Réunion",
    "ro": "Romania", "ru": "Russia", "rw": "Rwanda", "sh": "Saint Helena",
    "kn": "Saint Kitts and Nevis", "lc": "Saint Lucia", "pm": "Saint Pierre and Miquelon",
    "vc": "Saint Vincent and the Grenadines", "ws": "Samoa", "sm": "San Marino",
    "st": "São Tomé and Príncipe", "sa": "Saudi Arabia", "sn": "Senegal",
    "rs": "Serbia", "sc": "Seychelles", "sl": "Sierra Leone", "sg": "Singapore",
    "sx": "Sint Maarten", "sk": "Slovakia", "si": "Slovenia", "sb": "Solomon Islands",
    "so": "Somalia", "za": "South Africa", "kr": "South Korea", "ss": "South Sudan",
    "es": "Spain", "lk": "Sri Lanka", "sd": "Sudan", "sr": "Suriname",
    "se": "Sweden", "ch": "Switzerland", "sy": "Syria", "tw": "Taiwan",
    "tj": "Tajikistan", "tz": "Tanzania", "th": "Thailand", "tl": "Timor-Leste",
    "tg": "Togo", "tk": "Tokelau", "to": "Tonga", "tt": "Trinidad and Tobago",
    "tn": "Tunisia", "tr": "Turkey", "tm": "Turkmenistan", "tc": "Turks and Caicos Islands",
    "tv": "Tuvalu", "ug": "Uganda", "ua": "Ukraine", "ae": "United Arab Emirates",
    "uk": "United Kingdom", "us": "United States", "uy": "Uruguay", "uz": "Uzbekistan",
    "vu": "Vanuatu", "va": "Vatican City", "ve": "Venezuela", "vn": "Vietnam",
    "vg": "Virgin Islands (British)", "vi": "Virgin Islands (US)", "wf": "Wallis and Futuna",
    "ye": "Yemen", "zm": "Zambia", "zw": "Zimbabwe",
}

LANGUAGES: dict[str, str] = {
    "af": "Afrikaans", "ak": "Akan", "sq": "Albanian", "am": "Amharic",
    "ar": "Arabic", "hy": "Armenian", "az": "Azerbaijani", "eu": "Basque",
    "be": "Belarusian", "bn": "Bengali", "bho": "Bhojpuri", "bs": "Bosnian",
    "bg": "Bulgarian", "ca": "Catalan", "ceb": "Cebuano", "zh-CN": "Chinese (Simplified)",
    "zh-TW": "Chinese (Traditional)", "co": "Corsican", "hr": "Croatian",
    "cs": "Czech", "da": "Danish", "nl": "Dutch", "en": "English",
    "en-GB": "English (UK)", "en-US": "English (US)", "eo": "Esperanto",
    "et": "Estonian", "ee": "Ewe", "fil": "Filipino", "fi": "Finnish",
    "fr": "French", "gl": "Galician", "ka": "Georgian", "de": "German",
    "el": "Greek", "gn": "Guarani", "gu": "Gujarati", "ht": "Haitian Creole",
    "ha": "Hausa", "haw": "Hawaiian", "iw": "Hebrew", "hi": "Hindi",
    "hmn": "Hmong", "hu": "Hungarian", "is": "Icelandic", "ig": "Igbo",
    "id": "Indonesian", "ga": "Irish", "it": "Italian", "ja": "Japanese",
    "jw": "Javanese", "kn": "Kannada", "kk": "Kazakh", "km": "Khmer",
    "rw": "Kinyarwanda", "ko": "Korean", "ku": "Kurdish", "ky": "Kyrgyz",
    "lo": "Lao", "la": "Latin", "lv": "Latvian", "ln": "Lingala",
    "lt": "Lithuanian", "lg": "Luganda", "lb": "Luxembourgish", "mk": "Macedonian",
    "mg": "Malagasy", "ms": "Malay", "ml": "Malayalam", "mt": "Maltese",
    "mi": "Maori", "mr": "Marathi", "mn": "Mongolian", "my": "Myanmar (Burmese)",
    "ne": "Nepali", "no": "Norwegian", "ny": "Nyanja (Chichewa)", "or": "Odia",
    "om": "Oromo", "ps": "Pashto", "fa": "Persian", "pl": "Polish",
    "pt-BR": "Portuguese (Brazil)", "pt-PT": "Portuguese (Portugal)", "pa": "Punjabi",
    "qu": "Quechua", "ro": "Romanian", "ru": "Russian", "sm": "Samoan",
    "sr": "Serbian", "sn": "Shona", "sd": "Sindhi", "si": "Sinhala",
    "sk": "Slovak", "sl": "Slovenian", "so": "Somali", "st": "Southern Sotho",
    "es": "Spanish", "su": "Sundanese", "sw": "Swahili", "sv": "Swedish",
    "tg": "Tajik", "ta": "Tamil", "tt": "Tatar", "te": "Telugu",
    "th": "Thai", "ti": "Tigrinya", "ts": "Tsonga", "tr": "Turkish",
    "tk": "Turkmen", "uk": "Ukrainian", "ur": "Urdu", "ug": "Uyghur",
    "uz": "Uzbek", "vi": "Vietnamese", "cy": "Welsh", "xh": "Xhosa",
    "yi": "Yiddish", "yo": "Yoruba", "zu": "Zulu",
}

DEFAULT_COUNTRY = "uk"
DEFAULT_LANGUAGE = "en-GB"


def get_country(code: str | None) -> str:
    """A valid `gl` code - `code` if recognized, else the United Kingdom
    default. Never raises, so a tampered or stale form value degrades
    instead of failing a scrape."""
    code = (code or "").strip().lower()
    return code if code in COUNTRIES else DEFAULT_COUNTRY


def get_language(code: str | None) -> str:
    """A valid `hl` code - `code` if recognized (case preserved, since hl
    values like "pt-BR" are case-sensitive), else the UK-English default."""
    stripped = (code or "").strip()
    for known in LANGUAGES:
        if known.lower() == stripped.lower():
            return known
    return DEFAULT_LANGUAGE


def home_url(country_code: str, language_code: str) -> str:
    """The Google home page to start a search from, targeting `country_code`
    (gl) and `language_code` (hl). Every region goes through google.com
    with these as query parameters, rather than a per-country domain table
    (google.co.uk, google.de, ...) - Google documents gl/hl as the
    supported mechanism for all of them, and most country-specific domains
    just redirect to google.com now anyway."""
    return f"https://www.google.com/?hl={language_code}&gl={country_code}&pws=0"
