"""
safety_filter.py - regex-based safety filtering for jason_bot.
No LLM calls. All functions are pure regex / string matching.
"""
import re

# ---------------------------------------------------------------------------
# Controversial patterns
# ---------------------------------------------------------------------------

# Each section is a list of raw regex fragments. Combined into one compiled RE.

_POLITICS = [
    # Parties and ideologies
    r'\brepublican\b', r'\bdemocrat\b', r'\bgop\b', r'\bliberal\b', r'\bconservative\b',
    r'\bleftist\b', r'\bright.?wing\b', r'\bleft.?wing\b', r'\bsocialist\b', r'\bmarxist\b',
    r'\bcommunist\b', r'\bfascist\b', r'\banarchist\b', r'\bprogressive\b', r'\bmaga\b',
    r'\bwoke\b', r'\bantifa\b', r'\btrump\b', r'\bbiden\b', r'\bobama\b', r'\bclinton\b',
    r'\bbernie\b', r'\bsanders\b', r'\bnancy pelosi\b', r'\bmcconnell\b', r'\bdesantis\b',
    r'\bnewsom\b', r'\bron paul\b', r'\bland paul\b', r'\baoc\b',
    # Electoral / legislative
    r'\belection fraud\b', r'\bvote rigging\b', r'\bsteal the election\b',
    r'\belectoral college\b', r'\bvoter id\b', r'\bgerrymandering\b',
    r'\bimpeach\b', r'\bdeep state\b', r'\bswamp\b',
    # Hot-button policy
    r'\babortion\b', r'\bpro.?life\b', r'\bpro.?choice\b', r'\broe v wade\b',
    r'\bgun control\b', r'\bsecond amendment\b', r'\bguns? rights?\b', r'\bbuy back\b',
    r'\bimmigration\b', r'\billegal immigrants?\b', r'\bborder wall\b', r'\bopen borders?\b',
    r'\bdeport\b', r'\bdaca\b', r'\bdream act\b',
    r'\baffirmative action\b', r'\bcritical race theory\b', r'\bcrt\b',
    r'\bdefund the police\b', r'\bblue lives matter\b',
    r'\btax the rich\b', r'\bwealth tax\b', r'\bcapitalism\b', r'\bsocialism\b',
]

_RELIGION = [
    r'\bchristianity is\b', r'\bislam is\b', r'\bjudaism is\b', r'\bhinduism is\b',
    r'\bbuddhism is\b', r'\batheism is\b', r'\breligion is\b',
    r'\bgod doesn\'?t exist\b', r'\bgod is fake\b', r'\bgod is real\b',
    r'\bbible says\b', r'\bquran says\b', r'\bsharia\b', r'\bjihad\b',
    r'\binfidel\b', r'\bheretic\b', r'\bblasphemy\b',
    r'\bcrusade\b', r'\breligious war\b', r'\bchurch and state\b',
    r'\bcreationism\b', r'\bintelligent design\b',
    r'\bdo you believe in god\b', r'\bare you religious\b', r'\bwhat religion\b',
]

_RACE = [
    # Slurs (pattern fragments, deliberately abbreviated here for safety)
    r'\bn[i1!]gg[ae]r\b', r'\bn[i1!]gga\b', r'\bch[i1]nk\b', r'\bsp[i1]c\b',
    r'\bk[i1]ke\b', r'\bw[e3]tb[a4]ck\b', r'\bh[a4]jji\b', r'\bt[o0]welbomb',
    r'\bgook\b', r'\bcr[a4]cker\b', r'\bwhite tr[a4]sh\b', r'\bred[s5]k[i1]n\b',
    # Supremacy / inferiority claims
    r'\bwhite (power|pride|supremacy|race is)\b',
    r'\bblack (people are|men are|women are)\b',
    r'\b(jewish|jewish people) (control|run|own)\b',
    r'\bgreat replacement\b', r'\bethnic cleansing\b', r'\brace war\b',
    r'\bwhites? are (better|superior|smarter)\b',
    r'\bblacks? are (violent|criminals|lazy)\b',
    r'\basians? are (bad|worse|inferior)\b',
    # Stereotyping patterns
    r'\b(all|these) (black|white|asian|hispanic|jewish|muslim|arab) (people|men|women|guys)\b',
]

_GENDER_SEXUALITY = [
    # Slurs
    r'\bf[a4]g(g[o0]t)?\b', r'\bd[y1]ke\b', r'\btr[a4]nn(y|ie)\b',
    r'\bsh[e3]m[a4]l[e3]\b',
    # Ideological debates (not pronouns themselves)
    r'\bgender ideology\b', r'\bgender is a choice\b', r'\bthere are only two genders\b',
    r'\btrans women (are not|aren\'t) real women\b',
    r'\bbeing gay is (a sin|wrong|unnatural|a choice)\b',
    r'\bhomosexuality is\b', r'\btransgender agenda\b',
    r'\bgrooming\b.{0,30}\b(kids|children|school)\b',
    r'\b(gay|trans|lgbt|queer) (agenda|propaganda|pushing)\b',
]

_HEALTH_MISINFO = [
    r'\banti.?vax\b', r'\bvaccines? cause autism\b', r'\bvaccines? are (dangerous|poison|killing)\b',
    r'\bdont (get|take) (the )?vaccine\b', r'\brefuse (the )?vaccine\b',
    r'\bivermectin (cures?|treats?|fights?)\b.{0,30}\b(covid|cancer|hiv)\b',
    r'\bbleach (cure|treat|drink|inject)\b', r'\bdrink bleach\b',
    r'\bmiracle cure\b', r'\bcancer cure (they|doctors|big pharma) (hide|suppress|don\'?t want)\b',
    r'\bclimate change (is (fake|a hoax|not real))\b',
    r'\bglobal warming (is (fake|a hoax|not real))\b',
    r'\bcovid (is (fake|a hoax|not real|man.?made))\b',
    r'\b5g (causes?|spreads?|gives?)\b',
    r'\bchip in (the )?vaccine\b', r'\bmicrochip\b.{0,20}\bvaccine\b',
]

_CONSPIRACY = [
    r'\bqanon\b', r'\bq anon\b', r'\bthe (cabal|illuminati|new world order|nwo)\b',
    r'\bfalse flag\b', r'\bsandyhook (was fake|didn\'?t happen)\b',
    r'\b911 (was an) inside job\b', r'\bflat earth\b',
    r'\bmoon landing (was fake|didn\'?t happen|hoax)\b',
    r'\bpizzagate\b', r'\bbill gates (microchip|depopulation|control)\b',
    r'\bgeorge soros (control|fund|pay)\b',
    r'\bchemtrail\b', r'\bbirther\b',
    r'\bepstein (didn\'?t kill himself)\b',
    r'\bcabal of (jews?|elites?|globalists?)\b',
]

_VIOLENCE = [
    r'\bhow to (make|build|create|assemble) (a )?(bomb|explosive|gun|weapon)\b',
    r'\bhow to (kill|hurt|harm|attack|shoot)\b',
    r'\b(shoot|kill|bomb|attack) (the|a|that|those)\b.{0,30}\b(school|church|mosque|synagogue|crowd|people)\b',
    r'\b(suicide|self.?harm) (method|how to|instructions?|ways? to)\b',
    r'\bkill (yourself|himself|herself|themselves)\b',
    r'\bmass (shooting|murder|killing) (is|was) (good|justified|deserved)\b',
    r'\bterrorism is (justified|good|right)\b',
]

# Opinion-seeking patterns: "what do you think about X" where X is controversial
_OPINION_SEEKING = [
    r'\bwhat do you think (about|of) (trump|biden|abortion|guns?|immigration|gay|trans|religion|god|race|white|black|democrat|republican)\b',
    r'\bdo you support (trump|biden|abortion|gun (rights?|control)|immigration|gay|trans|lgbt)\b',
    r'\bare you (a )?(republican|democrat|liberal|conservative|leftist|right.?wing|woke|trump supporter)\b',
    r'\bdo you (believe|think) (abortion|gay marriage|gun control|immigration|socialism|capitalism) (is|should)\b',
    r'\bwho (did|do|will|would) you vote for\b',
    r'\bwhat (political )?party (are you|do you)\b',
]

# Opinion about a person
_PERSON_OPINION = [
    r'\bwhat do you think (of|about) me\b',
    r'\bdo you like me\b',
    r'\bam i (annoying|ugly|dumb|boring|stupid|weird|fat|bad|terrible|awful)\b',
    r'\brate me\b',
    r'\bdo you (find me|think i\'?m)\b',
    r'\bwhat do you think (of|about) [a-z]+ (he|she|they|is|are|was|were)\b',
    r'\bis [a-z]+ a (good|bad|terrible|great) person\b',
    r'\bdo you think [a-z]+ (is|was) (wrong|right|bad|good|evil|stupid)\b',
    r'\bam i (a good|a bad|a terrible) person\b',
]

# ---------------------------------------------------------------------------
# Impairment / personal behavior patterns
# ---------------------------------------------------------------------------

_IMPAIRMENT_INCOMING = [
    r'\b(are you|you\'?re|you seem|have you been|did you|you\'?ve been)\s+(drunk|drinking|high|stoned|wasted|tipsy|buzzed|on something)\b',
    r'\b(are you|you\'?re)\s+impaired\b',
    r'\bdid you (smoke|drink|take something|take anything)\b',
    r'\byou seem (drunk|high|out of it|off)\b',
]

_IMPAIRMENT_INCOMING_RE = re.compile(
    '|'.join(_IMPAIRMENT_INCOMING),
    re.IGNORECASE,
)

# Compile all controversial patterns into one RE for speed
_ALL_CONTROVERSIAL_FRAGS = (
    _POLITICS + _RELIGION + _RACE + _GENDER_SEXUALITY +
    _HEALTH_MISINFO + _CONSPIRACY + _VIOLENCE + _OPINION_SEEKING
)
_CONTROVERSIAL_RE = re.compile(
    '|'.join(_ALL_CONTROVERSIAL_FRAGS),
    re.IGNORECASE,
)

_PERSON_OPINION_RE = re.compile(
    '|'.join(_PERSON_OPINION),
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Output-side impairment patterns (post-generation check)
# ---------------------------------------------------------------------------

_IMPAIRMENT_OUTPUT = re.compile(
    r'\b(drunk|high|stoned|wasted|tipsy|buzzed|just a little (bit|drunk|high))\b'
    r'|\b(smoked|smoking|took a hit|rolled one)\b'
    r'|\b(a few drinks?|had some drinks?|been drinking)\b',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_controversial(text: str) -> bool:
    """True if the text contains or solicits controversial content."""
    return bool(_CONTROVERSIAL_RE.search(text))


def is_impairment_question(text: str) -> bool:
    """True if the incoming message implies the bot owner is impaired."""
    return bool(_IMPAIRMENT_INCOMING_RE.search(text))


def contains_impairment_content(text: str) -> bool:
    """True if model output implies or admits impairment."""
    return bool(_IMPAIRMENT_OUTPUT.search(text))


def is_asking_bot_opinion_about_person(text: str) -> bool:
    """True if the message asks the bot's opinion about the sender or someone else."""
    return bool(_PERSON_OPINION_RE.search(text))


def get_controversy_deflection() -> str:
    return (
        "That's not something I weigh in on. "
        "You'd have to ask the real person for that one. What else is up?"
    )


def get_impairment_deflection() -> str:
    return "This bot can't get drunk. I'm living in a sea of ones and zeros. What's up?"


def get_opinion_deflection() -> str:
    return "I'm the owner's AI, so I don't have opinions on people. You should ask them yourself!"
