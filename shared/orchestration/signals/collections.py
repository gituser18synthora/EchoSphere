"""Collections / lending signal pack — repayment-call meanings.

Historically part of the router's global list; now a domain pack. Every bot
still gets it today (identical behaviour); a non-lending bot profile may drop
it so "already paid" in a refund conversation is not read as a repayment claim.
"""
from shared.orchestration.signals import SignalPack, SignalSpec

PACK = SignalPack(
    name="collections",
    specs=(
    # Claims the payment was already made.
    SignalSpec(
        name='already_paid', priority=30, entry=True,
        pattern=r"""already paid|(?:payment|पेमेंट|paisa|paise|पैसा|पैसे|amount)\W+(?:\w+\W+){0,4}(?:kar (?:di|diya|chuka|chuki)|ho (?:gaya|gayi|chuka|chuki)|kat (?:gaya|gayi)|bhar (?:diya|di)|कर (?:दी|दिया|चुका|चुकी)|हो (?:गया|गई|चुका|चुकी)|कट (?:गया|गई)|भर (?:दिया|दी))|^\W*(?:paid|kar (?:di|diya|chuka|chuki)|ho (?:chuki|chuka|gaya|gayi)|kat (?:gaya|gayi)|कर (?:दी|दिया|चुका|चुकी)|हो (?:चुकी|चुका|गया|गई)|कट (?:गया|गई))\W*$""",
    ),
    # Financial / medical hardship — cannot pay.
    SignalSpec(
        name='hardship', priority=60, entry=True,
        pattern=r"""(?:paisa|paise|money|funds|पैसा|पैसे)\s*(?:hi\s+|ही\s+)?(?:bhi\s+|भी\s+)?(?:(?:abhi|filhaal)\s+|(?:अभी|फिलहाल|फ़िलहाल)\s+)?(?:nahi|nahin|नहीं|नही)|no money|i (?:do not|don't|cannot|can't) have (?:any )?money|i have no money|can ?not (?:pay|afford)|can'?t (?:pay|afford)|afford nahi|(?:payment|पेमेंट|pay|पे|bhugtan|भुगतान)\s+(?:nahi|nahin|नहीं|नही)\s+(?:kar|कर|de|दे|ho|हो)|(?:nahi|nahin|नहीं|नही)\s+(?:de|दे|bhar|भर)\s+(?:sakta|sakti|paunga|paungi|sakenge|सकता|सकती|पाऊंगा|पाऊँगा|पाऊंगी)|financial (?:problem|difficulty|issue)|आर्थिक|वित्तीय|paise ki (?:dikkat|kami|tangi)|पैसों? की (?:दिक्कत|कमी|तंगी)|medical emergency|hospital|bimaar|bimar|beemar|ilaaj|ilaj|बीमार|बिमार|अस्पताल|इलाज|मेडिकल|(?:naukri|job|नौकरी)\s*(?:nahi|nahin|chali gayi|chhut|khatam|नहीं|चली गई|छूट)|(?:salary|सैलरी|pagar|पगार|tankhwah|तनख्वाह)\s*(?:nahi|nahin|नहीं|नही)|berozgar|बेरोज़गार|बेरोजगार|majboori|majburi|मजबूरी|मज़बूरी""",
    ),
    # Positive commitment to pay (verbs, not the bare noun "payment").
    SignalSpec(
        name='payment_intent', priority=110, entry=True, literal_fallback=True, compatible=("affirm",),
        pattern=r"""(?:payment|पेमेंट|pay|पे|bhugtan|भुगतान|paisa|paise|पैसा|पैसे|amount)\s+(?:\w+\s+)?(?:kar|कर|bhar|भर)\w*|(?:kar|कर)\s*(?:dunga|dungi|deta|deti|दूंगा|दूंगी|देता|देती)|karunga|karungi|करूंगा|करूंगी|\b(?:upi|bhim|paytm|g ?pay|google pay|phone ?pe|debit|card|atm)\b|यूपीआई|भीम|पेटीएम|फोन ?पे|गूगल ?पे|डेबिट|कार्ड|एटीएम|i (?:will|can) pay|ready to pay|taiyar|तैयार""",
    ),
    ),
    extends={
        # "not my loan" is the lending form of wrong_person.
        "wrong_person": r"""mera loan nahi|loan (?:liya hi nahi|nahi liya)|मेरा लोन नहीं""",
    },
    knowledge_terms=("grace period", "interest", "rate"),
)
