"""TTS-ready values for the four spoken merchant fields (api/speech.py).

Expectations are real values from the 2026-11-10 export.  Run:
    env/bin/pytest tenant/Wordline/tests -q
"""

from __future__ import annotations

import json

import pytest

from tenant.Wordline.api.speech import spoken_address, spoken_model, spoken_name
from tenant.Wordline.scripts import convert_csv


@pytest.mark.parametrize("raw,expected", [
    ("AGARWAL PUBLIC SCHOOL", "Agarwal Public School"),
    ("BHANU MEDICAL AND GENERAL STORE", "Bhanu Medical and General Store"),
    ("TRISTAR CARS PVT LTD", "Tristar Cars Private Limited"),
    ("INDIAN ARTISAN PROP TAHIR AHMAD", "Indian Artisan Proprietor Tahir Ahmad"),
    ("PALLADIAN HOSPITALITY LLP", "Palladian Hospitality LLP"),          # acronym kept
    ("MMTC PAMP INDIA", "MMTC Pamp India"),                               # no-vowel token kept
    ("AFFWA L HQ WAC U AF", "AFFWA L HQ WAC U AF"),                       # all acronyms
    ("LIBERTY DRY FRUITS AND GENERAL STORES", "Liberty Dry Fruits and General Stores"),
    ("A H ARTS", "A H Arts"),                                             # initials kept
    ("K C TIWARI", "K C Tiwari"),
    ("SHRINIVAS M R", "Shrinivas M R"),
    ("Prasanthan K. C", "Prasanthan K C"),
    ("Kunal Patel", "Kunal Patel"),                                       # already natural
    ("TRIFED", "Trifed"),
    ("", ""),
    (None, ""),
])
def test_spoken_name(raw, expected):
    assert spoken_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("PAYTIVO-7210-2G+WIFI", "Paytivo 7210 2G plus WiFi"),
    ("Paytivo 7210-Hand held GPRS", "Paytivo 7210 Handheld GPRS"),
    ("ANTERA AL9220U-Hand held GPRS", "Antera AL9220U Handheld GPRS"),   # model number kept
    ("ANTERA A9210-Hand held GPRS", "Antera A9210 Handheld GPRS"),
    ("LINURA 7220-Hand held GPRS", "Linura 7220 Handheld GPRS"),
])
def test_spoken_model(raw, expected):
    assert spoken_model(raw) == expected


@pytest.mark.parametrize("raw,person,expected", [
    # metadata lines dropped, "INDORE MP / INDORE - 452016, MADHYA PRADESH" said once
    ("AGARWAL PUBLIC SCHOOL\nBICHOLI MARDANA ROAD\nINDORE MP\nINDORE - 452016, MADHYA PRADESH\n"
     "New ME Contact No.9303333356\nTerminal Line No.0", "K C TIWARI",
     "Agarwal Public School, Bicholi Mardana Road, Indore, Madhya Pradesh 452016"),
    # labels → "…, State PIN"; a line ending in a house number continues
    ("SHOP NO 17, SAIFEE PARK, 680\nDR. MASCARENHAS ROAD, MAZGAON , MUMBAI\nMerchant Pin Code\n"
     "400010\nMerchant State\nMAHARASHTRA", "ALI H",
     "Shop Number 17, Saifee Park, 680 Dr. Mascarenhas Road, Mazgaon, Mumbai, Maharashtra 400010"),
    # inline labels
    ("WARD NO 12 SHOP NO 14 GOLA BAZAR SHERGHATI GAYA \nMerchant Pin Code - 824211 "
     "Merchant State - BIHAR", "NITISH KUMAR",
     "Ward Number 12 Shop Number 14 Gola Bazar Sherghati Gaya, Bihar 824211"),
    # repeated "WARDHA 442001" said once
    ("HOUSE NO.14, WARD NO 1 GAJANAN NAGAR ARVI ROAD,\nGRAMPANCHAYAT PIPRI NEGHE WARDHA 442001\n"
     "WARDHA - 442001, MAHARASHTRA", "TARUN LAXMANDAS BHATIA",
     "House Number 14, Ward Number 1 Gajanan Nagar Arvi Road, Grampanchayat Pipri Neghe Wardha, "
     "Maharashtra 442001"),
    # G/F, BLDG, RD, a 10-digit property number kept as digits
    ("SHOP NO 6 G/F TUSHAR RESIDENCY BLDG\nNO A2 MILKAT NO 1060302347 PIMPLE SOUDAGAR RD "
     "PIMPRIGAON PIMPRI PUNE\nMerchant Pin Code\n411027\nMerchant State\nMAHARASHTRA", "KAMLESH SOLANKI",
     "Shop Number 6 Ground Floor Tushar Residency Building Number A2 Milkat Number 1060302347 "
     "Pimple Soudagar Road Pimprigaon Pimpri Pune, Maharashtra 411027"),
    # a trailing line repeating the contact person; a line break inside "Sarojini Devi Road"
    ("36 DECCAN CHRONICLE HOLDINGS LTD SAROJINI DEVI\nROAD SECUNDERABAD\nMerchant Pin Code\n500003\n"
     "Merchant State\nTELANGANA\nVENKATA SRINIVASA SESHA SAI VANG", "VENKATA SRINIVASA SESHA SAI VANG",
     "36 Deccan Chronicle Holdings Limited Sarojini Devi Road Secunderabad, Telangana 500003"),
    # the pin already in the address is not repeated; duplicate "New Delhi" segment
    ("6 46 OLD BAZAR STREET CHOPPADANDI KARIMANGAR 505415\nMerchant Pin Code\n505415\n"
     "Merchant State\nTELANGANA", "", "6 46 Old Bazar Street Choppadandi Karimangar, Telangana 505415"),
    ("\nA1 307,SAFDARJUNG ENCLAVE,NEW DELHI,NEW DELHI", "Gaurav Gupta",
     "A1 307, Safdarjung Enclave, New Delhi"),
    ("217 SUNSET RAW HOUSE OPP IDBI BANK GURUKUL ROAD DRIVE IN ROAD MEMNAGAR Ahmedabad Ahmedabad "
     "380052", "Kunal Patel",
     "217 Sunset Raw House Opposite IDBI Bank Gurukul Road Drive in Road Memnagar Ahmedabad 380052"),
    ("GRAND MAJESTIC MALL GANDHI NAGAR MAJESTIC BANGALORE\nGANDHI NAGAR MAJESTIC BANGALORE 560009\n"
     "Merchant Pin Code\n560009\nMerchant State\nKARNATAKA", "USHA",
     "Grand Majestic Mall Gandhi Nagar Majestic Bangalore, Karnataka 560009"),
    ("DOOR NO 61 INDIRA NAGAR ZONE 2 MADURAI 625020 TAMIL NADU", "",
     "Door Number 61 Indira Nagar Zone 2 Madurai, Tamil Nadu 625020"),
    ("SHOP NO AVON PLAZA THAKUR COMPLEX KANDIVALI EAST POISAR EAST", "",
     "Shop Number Avon Plaza Thakur Complex Kandivali East Poisar East"),
    ("Ground floor shop no 16 A floor B block Cushrow Baug SBS Road Electric house MUMBAI", "",
     "Ground floor shop number 16 A floor B block Cushrow Baug SBS Road Electric house Mumbai"),
    ("", "", ""),
    (None, None, ""),
])
def test_spoken_address(raw, person, expected):
    assert spoken_address(raw, person) == expected


def test_every_real_value_is_idempotent_and_never_invents_numbers():
    import re

    records = json.loads(convert_csv.JSON_PATH.read_text(encoding="utf-8"))["records"]
    assert len(records) == 42
    for r in records:
        for fn, value in ((spoken_name, r["outlet_name"]), (spoken_name, r["contact_person"]),
                          (spoken_model, r["asset_type"])):
            once = fn(value)
            assert fn(once) == once
        address = spoken_address(r["address"], r["contact_person"])
        assert spoken_address(address, r["contact_person"]) == address
        # every number spoken is one the export holds (labels/metadata may drop some)
        raw_numbers = set(re.findall(r"\d+", r["address"]))
        assert set(re.findall(r"\d+", address)) <= raw_numbers, (r["terminal_id"], address)
        # the registered mobile number never leaks into the spoken address
        assert r["contact_number"] not in address
