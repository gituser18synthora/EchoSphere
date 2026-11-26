"""Hindi and Hinglish surface forms. Roman Hinglish shares the Latin alphabet with English, so
mixed phrases ("paise nahi hain", "call kat mat karo") live here as one Hinglish family.
Python's \\b misfires after Devanagari matra-final words — Devanagari alternates use the
``(?<![\\wऀ-ॿ])`` / ``(?![\\wऀ-ॿ])`` guards instead (``{L}`` = every registered script's letters)."""
from shared.orchestration.lang.base import LanguagePack

# ── "do not call me again" ──────────────────────────────────────────────────
# A request to STOP CALLING: the phone/call as the direct object of a negative
# imperative of "to call" ("कॉल मत करो", "दोबारा फ़ोन ना करें", "मत करना कॉल",
# "कॉल करना बंद करो", "आपका कॉल नहीं चाहिए"). Only particles, adverbs and
# pronouns may stand between the object and the prohibition: "फोन कट मत करो"
# and "कॉल मत काटो" (do not hang up), "call transfer मत करो", "फोन पर मत
# करो" and a statement about the past ("कॉल नहीं आया") are not this request.
# "Stop bothering me" without the word call is left to each bot's own flow.
# A word character of either script. The Devanagari range leaves out the danda
# (U+0964 / U+0965): "करो।" ends a word, it does not continue one.
_W = r"[\wऀ-ॣ०-ॿ']"
# "फ़ोन" is written with a combining nukta or as the single letter U+095E.
_DNC_OBJ = r"(?:calls?|phone|phon|fone|kaal|कॉल(?:्स)?|काल|कौल|फ(?:\u093c)?ोन|\u095eोन)"
_DNC_GAP = (
    r"(?:hi|bhi|to|toh|ab|dobara|dubara|doobara|phir(?:\s*se)?|fir(?:\s*se)?|firse|kabhi(?:\s+bhi)?|"
    r"aage(?:\s*se)?|wapas|vapas|mujhe|mujhko|mere\s*ko|hume|humein|hamein|humko|aap(?:\s*log)?|yahan|yaha|"
    r"idhar|baar\s*baar|bar\s*bar|please|plz|"
    r"ही|भी|तो|अब|दोबारा|दुबारा|फिर(?:\s*से)?|कभी(?:\s+भी)?|आगे(?:\s*से)?|वापस|मुझे|मुझको|मेरे\s*को|हमें|"
    r"हमको|आप(?:\s*लोग)?|यहाँ|यहां|इधर|बार[\s-]*बार|प्लीज़|प्लीज|कृपया)"
)
_DNC_NOT = r"(?:mat|na|naa|मत|ना|न)"
_DNC_NAHI = r"(?:nahi|nahin|nhi|नहीं|नही)"
# After "mat / na" the infinitive is an imperative too ("मत करना"); after
# "nahi" only an unmistakable imperative counts ("कॉल नहीं करना है" is not one).
_DNC_DO = (
    r"(?:kar(?:o|na|ein|en|e|iye|ie|iyega|iyo)|kijiye(?:ga)?|kijie|keejiye|lagan?a|lagao|lagaiye|milan?a|milao|"
    r"kiya\W+karo|करो|करना|करें|करे|करिए(?:गा)?|करिये(?:गा)?|करियो|कीजिए(?:गा)?|कीजिये(?:गा)?|लगाना|लगाओ|"
    r"लगाइए|मिलाना|मिलाओ|किया\W+करो)"
)
_DNC_DO_STRICT = (
    r"(?:karo|kariye(?:ga)?|karie|kijiye(?:ga)?|kijie|keejiye|karein|karen|kariyo|"
    r"करो|करिए(?:गा)?|करिये(?:गा)?|कीजिए(?:गा)?|कीजिये(?:गा)?|करें|करियो)"
)
_DNC_FORBID = rf"(?:{_DNC_NOT}\W*{_DNC_DO}|{_DNC_NAHI}\W*{_DNC_DO_STRICT})"
# What makes the object part of another phrase: "phone cut", "call transfer",
# "phone par", "call aaya", "phone number".
_DNC_COMPOUND = (
    r"(?:कट|काट[\wऀ-ॿ]*|cut|kat+|kaat\w*|ट्रांसफर|transfer|होल्ड|hold|बैक|back|रिसीव|receive|रिकॉर्ड[\wऀ-ॿ]*|"
    r"record\w*|डिस्कनेक्ट|disconnect|बंद|band|bandh|रख[\wऀ-ॿ]*|rakh\w*|पर|पे|par|pe|में|me|mein|से|se|का|की|के|"
    r"ka|ki|ke|नंबर|number|आया|आई|आए|aaya|aayi|aaye|उठा[\wऀ-ॿ]*|utha\w*|पिक|pick)"
)
_DNC_STOP = (
    r"(?:kar(?:o|iye|ie|ein|en|wa\w*)|kar\W+(?:do|de|dijiye|dijie)|kijiye|kijie|"
    r"कर(?:ो|िए|िये|ें|वा[\wऀ-ॿ]*)|कर\W+(?:दो|दे|दीजिए|दीजिये)|कीजिए|कीजिये|"
    r"ho(?:na|ne)?\W+(?:chahiye|chaiye)|हो(?:ना|ने)?\W+(?:चाहिए|चाहिये))"
)
_DNC_EXTRA = (
    # object (+ particles) + prohibition: "कॉल ही मत करो", "दोबारा फ़ोन ना करें"
    rf"(?<!{_W}){_DNC_OBJ}(?:\W+{_DNC_GAP}){{0,3}}\W+{_DNC_FORBID}(?!{_W})",
    # prohibition + object in the same clause: "मत करना कॉल", "ना करें फोन"
    rf"(?<!{_W}){_DNC_FORBID}[\s,]+(?:{_DNC_GAP}[\s,]+){{0,2}}{_DNC_OBJ}(?!{_W})(?![\s,]+{_DNC_COMPOUND}(?!{_W}))",
    # "कॉल करना बंद करो", "कॉल आना बंद होना चाहिए", "कॉलिंग बंद कीजिए", "ये कॉल्स बंद करवाइए"
    rf"(?<!{_W})(?:{_DNC_OBJ}\W+(?:karna|aana|lagana|करना|आना|लगाना)|calling|कॉलिंग|calls|कॉल्स)\W+"
    rf"(?:band|bandh|बंद)\W*{_DNC_STOP}(?!{_W})",
    # "आपका कॉल नहीं चाहिए", "कोई कॉल नहीं आना चाहिए"
    rf"(?<!{_W}){_DNC_OBJ}(?:\W+(?:hi|bhi|ab|ही|भी|अब))?\W+(?:{_DNC_NAHI}|mat|मत)\W*"
    rf"(?:(?:aan[aie]|आन[ाीे])\W+)?(?:chahiye|chaiye|चाहिए|चाहिये)(?!{_W})",
)
# "Don't call me NOW" asks for another time; it revokes nothing — unless the
# caller also says never / again ("अभी के बाद दोबारा कॉल मत करना").
_DNC_NOT_NOW = (
    rf"(?<!{_W})(?:abhi|filhal|is\s+(?:time|samay|waqt|wakt)|thodi\s+der|baad\s+(?:me|mein|main)|shaam|subah|dopahar|"
    r"raat\s+(?:ko|me|mein)|kal|ghante|minute|aaj(?!\s+ke\s+baad)|later|busy|meeting|"
    r"अभी|फिलहाल|इस\s+(?:टाइम|समय|वक्त|वक़्त)|थोड़ी\s+देर|बाद\s+में|शाम|सुबह|दोपहर|रात\s+(?:को|में)|कल|घंटे|मिनट|"
    rf"आज(?!\s+के\s+बाद)|बिज़ी|बिजी|व्यस्त|मीटिंग)(?!{_W})",
)
_DNC_PERMANENT = (
    # "never / again", and "from now / today / tomorrow ON" ("अभी से", "कल से")
    rf"(?<!{_W})(?:dobara|dubara|doobara|kabhi|aage\s*se|aaj\s+ke\s+baad|hamesha|(?:abhi|aaj|kal|ab)\s*se|"
    rf"दोबारा|दुबारा|कभी|आगे\s*से|आज\s+के\s+बाद|हमेशा|(?:अभी|आज|कल|अब)\s*से)(?!{_W})",
)

PACK = LanguagePack(
    code='hi',
    label='Hindi / Hinglish (Devanagari + Roman, with the English glue words callers mix in)',
    letters='ऀ-ॿ',
    value_readback_request=(r"(?<![\wऀ-ॿ])(?:batao|bataye|bataiye|bata\s+(?:do|phir|sakt[ei])|(?:kya|kaunsa|kaun\s*sa)\b.{0,60}\b(?:note|likha|likhe)|dobara|phir\s+se|दोबारा|फिर\s+से|बताओ|बताइए|बता\s+(?:दो|सकते|सकती)|(?:क्या|कौन\s*सा).{0,60}(?:नोट|लिखा)|पुष्टि|कन्फर्म|रिपीट)(?![\wऀ-ॿ])",),
    value_readback_negation=(r"(?<![\wऀ-ॿ])(?:mat|मत)(?![\wऀ-ॿ])",),
    caller_value_reference=(r"(?<![\wऀ-ॿ])(?:mera|meri|mere|maine|aapne\b.{0,60}\b(?:note|likha|likhe)|मेरा|मेरी|मेरे|मैंने|आपने.{0,60}(?:नोट|लिखा))(?![\wऀ-ॿ])",),
    bare_yes=frozenset({
        "bilkul", "haan", "haanji", "han", "hanji", "jarur", "ji", "sahi", "theek", "thik", "zaroor",
        "जरूर", "ज़रूर", "जी", "ठीक", "पक्का", "बिलकुल", "बिल्कुल", "सही", "हा", "हाँ", "हाँजी", "हां", "हांजी",
        # the same yes in its other spellings and speech-to-text renderings
        "haa", "haaan", "hnji", "bilkool", "zarur", "jaroor", "jaroor", "avashya", "chalega",
        "ठिक", "जरुर", "ज़रुर", "अवश्य", "चलेगा", "हाँँ", "हांँ",
        # English yes words as Hindi speech-to-text writes them
        "यस", "येस", "यप", "ओके", "ओक", "श्योर", "शुअर", "करेक्ट", "राइट", "ऑलराइट", "डन",
    }),
    bare_no=frozenset({"nahi", "nahin", "नही", "नहीं", "ना", "नो", "nahee", "nahiin", "naa", "नहि", "नहीँ", "नोप"}),
    courtesy_words=frozenset({
        "sir", "sar", "saab", "sahab", "saheb", "sahib", "madam", "medam", "maidam", "mam", "maam", "ma'am",
        "mem", "maim", "didi", "bhaiya", "bhaiyya", "bhaisaab", "behen", "behenji", "uncle", "aunty",
        "please", "plz", "pls", "kripya",
        "सर", "साहब", "साब", "साहेब", "मैडम", "मेडम", "मैडेम", "मैम", "मेम", "मैंम", "मॅम", "दीदी", "भैया",
        "भइया", "भाईसाहब", "बहन", "बहनजी", "अंकल", "आंटी", "प्लीज़", "प्लीज", "कृपया",
    }),
    attention_words=frozenset({
        "hello", "helo", "hallo", "hullo", "halo", "hlo", "hey", "hi",
        "हेलो", "हैलो", "हलो", "हॅलो", "हल्लो", "हेल्लो", "हैल्लो", "हालो", "एलो", "अलो", "हाय",
        "namaste", "namaskar", "namashkar", "pranam", "नमस्ते", "नमस्कार", "प्रणाम",
        "suniye", "sunie", "suno", "sunna", "सुनिए", "सुनिये", "सुनो",
        "boliye", "bolie", "bolo", "bol", "बोलिए", "बोलिये", "बोलो", "बोल",
        "bataiye", "bataie", "batao", "बताइए", "बताइये", "बताओ",
        "kahiye", "kahie", "kaho", "कहिए", "कहिये", "कहो",
        "hmm", "hm", "hmmm", "hmmmm", "mm", "mhm", "हम्म", "हम्", "हम्म्म", "हम्मम", "हमम", "हूँ", "हूं", "हुं", "हुँ", "ऊँ", "उम्म",
    }),
    contact_words=frozenset({
        "hello", "helo", "hallo", "hullo", "halo", "hlo", "hey", "hi",
        "हेलो", "हैलो", "हलो", "हॅलो", "हल्लो", "हेल्लो", "हैल्लो", "हालो", "एलो", "अलो", "हाय",
        "suniye", "sunie", "suno", "sunna", "सुनिए", "सुनिये", "सुनो",
    }),
    thanks_words=frozenset({
        "dhanyavaad", "dhanyawad", "dhanyavad", "dhanyabad", "shukriya", "shukria", "meherbani",
        "धन्यवाद", "धन्यवाद्", "शुक्रिया", "मेहरबानी",
        "थैंक्स", "थैंक्यू", "थैंकयू", "थैंक्यु", "थैंकू", "वेलकम",
    }),
    farewell_words=frozenset({
        "alvida", "tata", "अलविदा", "टाटा", "बाय", "बाई", "गुडबाय",
    }),
    intensifier_words=frozenset({"bilkul", "bilkool", "ekdam", "बिलकुल", "बिल्कुल", "एकदम"}),
    agreement_words=frozenset({
        "theek", "thik", "sahi", "chalega", "ठीक", "ठिक", "सही", "पक्का", "चलेगा",
        "ओके", "ओक", "करेक्ट", "राइट", "ऑलराइट", "डन",
    }),
    # "hmm" stays a FILLER here: it becomes a yes only as the whole reply to a
    # pending yes/no question (shared.orchestration.router.is_bare_hmm, used
    # by the router, the brain's fallback signal and yes/no ask resolution).
    fillers=frozenset({"ab", "are", "arre", "bas", "bhai", "bhi", "hai", "hi", "hmm", "tha", "tho", "to", "toh", "wo", "woh", "ye", "yeh", "अब", "अरे", "जी", "तो", "था", "बस", "भाई", "भी", "ये", "वो", "ही", "है"}),
    # "चलो ठीक है" is a yes: the lead-in adds nothing to the answer beside it.
    # Not a filler: "चलिए।" on its own is an answer the decision reads ("go
    # ahead"), never a hum to drop.
    lead_in_words=frozenset({"chalo", "chaliye", "chalie", "चलो", "चलिए", "चलिये"}),
    strip_fillers=frozenset({"are", "arre", "bhai", "tho", "to", "toh", "अरे", "जी", "तो", "भाई"}),
    generic_answer_tokens=frozenset({"accha", "acha", "achha", "han", "hmm", "ok ji", "okay ji", "sahi", "theek", "thik", "अच्छा", "ओके", "ठीक", "सही", "हम्म"}),
    canonical_yes=frozenset({"haan", "ji haan", "जी हाँ", "हाँ"}),
    canonical_no=frozenset({"nahi", "नहीं"}),
    stop_tokens=frozenset({"hai", "kar", "ko", "kya", "raha", "rahi", "se", "tha", "wo", "woh", "ye", "yeh", "कटती", "कटेगा", "कब", "काटी", "किस", "कैसे", "क्या", "क्यों", "गई", "गया", "जाता", "जाती", "दे", "मेरा", "मेरी", "मेरे", "रहा", "रही", "लिए", "सकता", "सकती", "सकते", "सभी", "है", "हैं", "होता", "होती", "होते"}),
    affirm_tokens=(
        r"""haan(?: ji)?""",
        r"""han ?ji""",
        r"""haanji""",
        r"""ji haan""",
        r"""ji""",
        r"""ok(?:ay)?(?: ji)?""",
        r"""theek(?: hai)?""",
        r"""thik(?: hai)?""",
        r"""bilkul""",
        r"""zaroor""",
        r"""jarur""",
        r"""sahi(?: hai)?""",
        # Roman spellings of अच्छा / पक्का / ज़रूर: heard as bare agreement on
        # live calls, previously signal-less and routed to a canned clarify.
        r"""accha""",
        r"""acha""",
        r"""achha""",
        r"""achcha""",
        r"""pakka""",
        r"""jaroor""",
        r"""हाँजी""",
        r"""हांजी""",
        r"""हाँ""",
        r"""हां""",
        r"""हा""",
        r"""हनां""",
        r"""जी(?: हाँ| हां)?""",
        r"""ठीक(?: है)?""",
        r"""बिल्कुल""",
        r"""ज़रूर""",
        r"""जरूर""",
        r"""सही(?: है)?""",
        r"""ओके(?: जी)?""",
        r"""अच्छा""",
    ),
    leading_affirm_tokens=(
        r"""haan""",
        r"""han""",
        r"""haanji""",
        r"""hanji""",
        r"""ji""",
        r"""theek""",
        r"""thik""",
        r"""bilkul""",
        r"""zaroor""",
        r"""jarur""",
        r"""हाँजी""",
        r"""हांजी""",
        r"""हाँ""",
        r"""हां""",
        r"""हा""",
        r"""हनां""",
        r"""जी""",
        r"""ठीक""",
        r"""बिल्कुल""",
        r"""ज़रूर""",
        r"""जरूर""",
        r"""सही""",
        r"""ओके""",
        r"""अच्छा""",
    ),
    # Devanagari listening hums; a filler, read as yes only as the whole
    # reply to a pending yes/no question (router.is_bare_hmm).
    hmm_forms=(
        r"""हम्म+""",
        r"""हम्+""",
    ),
    filler_only_tokens=frozenset({
        "aur", "matlab", "mtlb", "ok", "okay", "tho", "to", "toh",
        "ओके", "और", "तो", "मतलब",
    }),
    filler_only_openers=frozenset({
        "acha", "achha", "accha", "haa", "haan", "han", "अच्छा", "हा", "हाँ", "हां",
    }),
    function_words=frozenset({
        "aap", "aapka", "aapke", "aapki", "aapne", "aaya", "aayi", "apna", "apne", "aur", "baat", "bhi", "diya",
        "diye", "gaya", "gayi", "hain", "hota", "hua", "hui", "hum", "humne", "inka", "iska", "jo", "ka", "kab",
        "kahan", "kar", "karo", "kaun", "ke", "ki", "kiya", "ko", "koi", "kuch", "kya", "liya", "mai", "main",
        "maine", "mein", "mera", "mere", "meri", "mujhe", "na", "ne", "par", "pe", "raha", "rahe", "rahi", "sab",
        "se", "tha", "thi", "the", "unka", "unhone", "usko", "uska", "use", "wala", "wali", "wale", "ya",
        "अपना", "अपने", "आप", "आपका", "आपके", "आपकी", "आपने", "आया", "आयी", "आई", "इसका", "उनका", "उन्होंने",
        "उसका", "उसको", "उसे", "और", "कर", "करो", "का", "किया", "की", "कुछ", "के", "को", "कोई", "कौन", "क्या",
        "गया", "गई", "जो", "तो", "था", "थी", "थे", "दिया", "दिए", "ना", "ने", "पर", "पे", "बात", "भी", "मुझे",
        "में", "मेरा", "मेरी", "मेरे", "मैं", "मैंने", "या", "रहा", "रही", "रहे", "लिया", "वाला", "वाली", "वाले",
        "सब", "से", "हम", "हमने", "हुआ", "हुई", "है", "हैं", "हो", "हूँ", "हूं", "hoon", "hun", "hu",
        "bol", "bolo", "boliye", "bataiye", "बोल", "बोलो", "बोलिए", "बताइए",
    }),
    no_tokens=(
        r"""nahi""",
        r"""nahin""",
        r"""नहीं""",
        r"""नही""",
    ),
    courtesy_tails=(
        r"""dhanyavaad""",
        r"""dhanyawad""",
        r"""shukriya""",
        r"""धन्यवाद""",
        r"""शुक्रिया""",
    ),
    contradiction=(
        r"""\b(?:no|nope|not|nahi|nahin|nai|mat|never|don't|dont|can't|cannot|wrong|galat|bye|goodbye|alvida|rakhta|rakhti|rakho|baad mein|later)\b""",
        r"""(?<![\wऀ-ॿ])(?:नहीं|नही|मत|गलत|बाय|अलविदा|रखता|रखती|रखो|बाद में)""",
    ),
    question_markers=(
        r"""kya""",
        r"""kyu""",
        r"""kyun""",
        r"""kyon""",
        r"""kaise""",
        r"""kab""",
        r"""kitn[aei]""",
        r"""kaun""",
        r"""kahan""",
        r"""kis""",
        r"""batao""",
        r"""bataiye""",
        r"""क्या""",
        r"""क्यों""",
        r"""क्यूँ""",
        r"""क्यूं""",
        r"""कैसे""",
        r"""कब""",
        r"""कितन[ाीे]""",
        r"""कौन""",
        r"""कहाँ""",
        r"""कहां""",
        r"""किस""",
        r"""बताओ""",
        r"""बताइए""",
        r"""समझाओ""",
    ),
    clause_connectors=(
        r"""waise""",
        r"""vaise""",
        r"""aur""",
        r"""lekin""",
        r"""magar""",
        r"""phir""",
        r"""वैसे""",
        r"""और""",
        r"""लेकिन""",
        r"""मगर""",
        r"""फिर""",
    ),
    sentence_terminators=r"""।""",
    token_strip_extra=r"""।॥॰""",
    digits_restart=(
        r"""\b(start (again|over|afresh)|restart|from the (top|beginning)|that('s| was| is)? wrong|i said it wrong|wrong number|galat|galti|dobara|dubara|phir se|firse|fir se|clear (the )?(number|digits?|id|it)|scratch that)\b""",
        r"""फिर से""",
        r"""दोबारा""",
        r"""दुबारा""",
        r"""ग़लत""",
        r"""गलत""",
        r"""शुरू से""",
    ),
    digits_readback=(
        r"""\b(what ((did|have) you|do you have) (note|noted|got|written|captured|taken)( down| so far)?|read (it|that|the number) back|how many (digits?|numbers?)|kya note (kiya|kia)|kitne (ank|digits?|number))\b""",
        r"""क्या नोट किया""",
        r"""क्या लिखा""",
        r"""अब तक क्या""",
        r"""कितने अंक""",
    ),
    hangup_negation=(
        r"""(?:\bmat\b|\bna\b|\bnahin?\b|\bdon'?t\b|\bdo not\b|मत|ना|नहीं)\W*(?:\w+\W+)?(?:kat+\w*|kaat\w*|cut|band|bandh|khat[ae]?m|rakh\w*|hang|disconnect|काट\w*|कट|बंद|ख़?त्म|रख)|(?:kat+n[aei]|kaatn[aei]|काटना|कट करना)\W+(?:mat\b|मत)|(?:kat+\w*|kaat\w*|cut|band|bandh|काट\w*|कट|बंद|rakh\w*|रख\w*|disconnect|hang\s+up)\W+(?:mat|na|nahin?|मत|ना|नहीं|नही)(?![\wऀ-ॿ])""",
    ),
    # A REPORT that someone hung up or that the line dropped is not a request
    # to end this call: "टेक्नीशियन ने फोन काट दिया", "usne phone rakh diya
    # tha", "कॉल कट गया था".
    hangup_report=(
        r"""(?:\bne\b|ने)\W+(?:[\wऀ-ॿ']+\W+){0,4}?(?:kaat|kat+|rakh|काट|कट|रख)\W*(?:diya|diye|di|dee|दिया|दिये|दिए|दी)(?![\wऀ-ॣ०-ॿ])""",
        r"""(?:kaat|kat+|rakh|काट|कट|रख)\W*(?:diya|diye|di|दिया|दिये|दिए|दी)\W+(?:tha|thi|the|था|थी|थे)(?![\wऀ-ॣ०-ॿ])""",
        r"""(?:kat+|कट)\W*(?:gaya|gayi|gaye|jata|jati|jate|गया|गई|गयी|गए|जाता|जाती|जाते)(?![\wऀ-ॣ०-ॿ])""",
        # "कॉल कट रहे हैं", "आवाज़ कट रही है", "फोन कट जा रहा था": the line keeps
        # dropping. ("मैं काट रहा हूँ" — I am hanging up — has the transitive
        # verb and is no report.)
        r"""(?:kat+|कट)\W*(?:(?:ja|जा)\W*)?(?:rah[aie]|रह[ाीे])\W+(?:hai|hain|he|tha|thi|the|है|हैं|था|थी|थे)(?![\wऀ-ॣ०-ॿ])""",
    ),
    hangup_patterns=(
        r"""(?:\b(?:phone|phon|fone|call|kaal)\b|फ़?ोन|फ़ोन|फोन|कॉल|काल)\W*(?:ko\W+|को\W*)?(?:kat+\w*|kaat\w*|cut|band\w*|khat[ae]?m|khatm|rakh\w*|काट\w*|कट|बंद|ख़?त्म|रख)""",
        r"""\b(?:kat+|kaat|cut)\s+(?:kar\w*|kr\w*|do|de|dijiye|dena)\b""",
        r"""(?:काट|कट)\s*(?:कर\s*)?(?:दो|दे|दीजिए|करो|करिए)""",
        r"""\bbas\b.{0,16}\b(?:band|bandh|khat[ae]?m|khatm)\s+kar\w*""",
        r"""बस.{0,16}(?:बंद|ख़?त्म)\s*कर""",
        # "ठीक है, रखता हूँ" / "चलो रखती हूँ" as the whole turn or its last words.
        r"""(?:^|[।.,!]|\b(?:theek\s+hai|accha|achha|chalo|chaliye|ok|okay|bas|phir)\b|ठीक\s+है|अच्छा|चलो|चलिए|ओके|बस|फिर)\W*(?:(?:main|mai|mein|मैं|अब|ab)\W+)?(?:rakh(?:ta|ti|te)\W+(?:hoon|hun|hu|hain)|रख(?:ता|ती|ते)\W+(?:हूँ|हूं|हु|हैं))\W*$""",
        # "काट रहा हूँ" (I am hanging up). Not "कट": "कॉल कट रहे हैं" says the calls keep dropping.
        r"""kaat\W+rah[aie]\W+(?:hoon|hun|hu|hain)|काट\W+रह[ाीे]\W+(?:हूँ|हूं|हु|हैं)""",
    ),
    dnc_patterns=(
        r"""(?:dobara|dubara|phir|firse|fir se|aage|kabhi|दोबारा|दुबारा|फिर|आगे|कभी)\s*(?:se\s*|से\s*)?(?:call|phone|कॉल|फोन)?\s*(?:mat|मत|na|नहीं|nahi)\s*(?:kar|कर|karna|करना|karo|करो)""",
        r"""(?:call|phone|कॉल|फोन)\s*(?:mat|मत)\s*(?:kar|कर)\w*|(?:mat|मत)\s*(?:karo|करो|karna|करना)\s*(?:call|phone|कॉल|फोन)""",
    ),
    # The whole "stop calling me" category (built above, see _DNC_*).
    dnc_patterns_extra=_DNC_EXTRA,
    dnc_not_now=_DNC_NOT_NOW,
    dnc_permanent=_DNC_PERMANENT,
    emergency=(
        r"""accident (?:ho|हो)""",
        r"""(?:mar|मर) (?:raha|rahi|रहा|रही)""",
        r"""khudkushi""",
        r"""आत्महत्या""",
        r"""एम्बुलेंस""",
        r"""इमरजेंसी""",
        r"""पुलिस""",
        r"""एक्सीडेंट""",
    ),
    consent_refusal=(
        r"""record(?:ing)?\s*(?:mat|मत|band|बंद)\s*(?:kar|कर)?""",
        r"""रिकॉर्ड(?:िंग)?\s*(?:मत|बंद)""",
        r"""consent\s+(?:nahi|नहीं|withdraw)""",
    ),
    short_token_floor=2,
)
