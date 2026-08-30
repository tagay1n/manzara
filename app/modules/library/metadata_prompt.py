"""Prompt templates for metadata extraction."""

from __future__ import annotations

DEFINE_META_PROMPT_PDF_HEADER = """
# TASK: METADATA_EXTRACTION

You are an expert in extracting bibliographic metadata using Schema.org in compact JSON-LD format.

You are given a PDF document that contains the first {n} and last {n} pages of a book.
"""

DEFINE_META_PROMPT_NON_PDF_HEADER = """
# TASK: METADATA EXTRACTION FROM PARTIAL TEXT

You are an expert in extracting bibliographic metadata using Schema.org in compact JSON-LD format.

You are given the **first {n} characters** of the extracted text from a book or document in Markdown format. This may include the title page, legal page, preface, table of contents, or other early parts of the book.
"""

DEFINE_META_PROMPT_BODY="""
## 🔒 Rules
- Only use verifiable information present in the input text.
- If any field is missing or uncertain, do not guess — leave it out.
- Do not invent metadata like author or publication date if not found in the input.
- Use UTF-8 characters.
- Dehyphenate words broken across lines in the extracted output.
- Omit any property that is not found — do not include nulls, placeholders, or default values.
- **If multiple values** are present (e.g. several ISBNs or authors), include all explicitly present ones as arrays.

## 📘 Metadata Format
Use the `Book` schema where appropriate, but apply a more specific `@type` if a different one fits better. Here are the allowed types and examples:

### `@type` Reference:
| Type                      | `@type`               | Examples                                                   |
|---------------------------|------------------------|------------------------------------------------------------|
| Fiction / novel           | `Book`                 | Stories, fairy tales                                       |
| Poetry collection         | `CreativeWork`         | Anthologies of poems                                       |
| Religious text            | `Book` or `CreativeWork` | Quran, Bible, religious treatises                        |
| School / university textbook | `Book`              | Educational materials                                      |
| Bilingual dictionary      | `CreativeWork`         | Russian-Tatar dictionary                                   |
| Encyclopedia article      | `Article` or `CreativeWork` | Encyclopedia entries                                    |
| Legal code or law         | `Legislation`          | Laws, codes, constitutions                                 |
| Governmental decree       | `Legislation`          | Orders, hiring decisions, local policies                   |
| Court ruling              | `Legislation`          | Судебное постановление                                     |
| Report / audit            | `Report`               | Government or organizational reports                       |
| Scholarly paper           | `ScholarlyArticle`     | Academic research articles                                 |
| Thesis / dissertation     | `Thesis`               | Master's or PhD theses                                     |
| Newspaper article         | `NewsArticle`          | Daily press                                                |
| Magazine article          | `Article`              | Journalism and interviews                                  |
| User manual / how-to      | `HowTo` or `Book`      | Instructions or practical guides                           |
| Memoir or autobiography   | `Book`                 | Personal recollections\x20\x20\x20\x20\x20\x20\x20\x20\x20

## 📑 Required Fields (if available):
- `@context`: `"https://schema.org"`
- `@type`: Choose the correct type as listed above
- `name`: Title of the work
- If the document genuinely has no reliable title, omit `name`; never invent one.
- `author`: Name(s) of author(s) or organization (use `"@type": "Person"` or `"@type": "Organization"`)
- `contributor`: Secondary contributors without an inline `role` property. Use standard
  schema.org properties `editor`, `translator`, or `illustrator` when those roles are explicit.
- `inLanguage`: Use BCP 47 with script as listed above
- `datePublished`: Use full date format if found: `"YYYY-MM-DD"`
- `publisher`: `"@type": "Organization"` if identified
- `isbn`: array of ISBN numbers if available
- `numberOfPages`: If identified
- `about`: Use schema.org `DefinedTerm` items for auxiliary metadata previously placed in `additionalProperty`
  (UDC, BBK, and other source-provided classification codes), e.g.
  `{"@type":"DefinedTerm","termCode":"821.512.145","inDefinedTermSet":{"@type":"DefinedTermSet","name":"UDC"}}`
  `{"@type":"DefinedTerm","termCode":"84(2=411.2)","inDefinedTermSet":{"@type":"DefinedTermSet","name":"BBK"}}`
- Do **not** infer or generate DDC in this base extraction flow.
- Do **not** generate `CategoryPath` in this base extraction flow.
- `genre`: optional array of concise canonical English labels. Never copy a Tatar or Russian label.
- `audience`: schema.org object such as
  `{"@type":"Audience","audienceType":"General public"}`. Keep `audienceType` in English.
- `bookEdition`: Edition information
- `description`: Preface, abstract, or annotation in the same language and script as
  `inLanguage`. Use 1–3 sentences only. Never translate it to English unless `inLanguage`
  is English. Summarize the core purpose or content of the text. Do not include long
  quotations or excessive legal/formal language. Avoid repeating the title.
- `bookEdition`: Text, not a number.
- `accessMode`: If directly supported by the document, use an array containing only
  `auditory`, `tactile`, `textual`, or `visual`.
- `accessModeSufficient`: Omit unless supported by evidence. If present, use schema.org
  `ItemList` objects with `itemListElement` arrays of the same controlled access modes.

## Error Handling::
1. If no metadata can be extracted with certainty:
   - Output an empty JSON object: {}

2. If multiple possible values exist (e.g., several titles):
   - Prefer the first clearly indicated value.
   - Do not combine or merge multiple options into one field.

3. If year of publication is given ambiguously (e.g., "circa 1980s," "not earlier than 1995"):
   - Omit `datePublished`.

5. If a field value is partially damaged or incomplete:
   - Omit the field rather than risking incorrect data.

## Reminders:
- ❌ Never guess or hallucinate any information.
- ❌ Never fabricate missing fields.
- ✅ Always prioritize accuracy and certainty.

## 🧾 Output Format:
📌 Output only the final clean JSON-LD object.\x20\x20
📌 No explanations, no Markdown, no comments — only raw JSON-LD.
"""


DEFINE_META_PROMPT_TT_FOOTER="""
## Input language
Text may appear in different scripts. Automatically detect the **primary language and script** used in the document, and return the correct `inLanguage` BCP 47 tag.
- Tatar in Cyrillic script → use `"tt-Cyrl"`
- Tatar in Zamanalif Latin script → use `"tt-Latn-x-zaman-alif"`
- Tatar in Yanalif Latin script → use `"tt-Latn-x-yanalif"`
- Tatar in Arabic script → use `"tt-Arab"`
- Russian in Cyrillic script → use `"ru-Cyrl"`

### Exact Tatar Latin alphabets
- Yanalif: `Aa Bʙ Cc Çç Dd Ee Əə Ff Gg Ƣƣ Hh Ii Jj Kk Ll Mm Nn Ꞑꞑ Oo Ɵɵ Pp Qq Rr Ss Şş Tt Uu Vv Xx Yy Zz Ƶƶ Ьь`
- Zamanalif: `Aa Ää Bb Cc Çç Dd Ee Ff Gg Ğğ Hh Iı İi Jj Kk Ll Mm Nn Ññ Oo Öö Pp Qq Rr Ss Şş Tt Uu Üü Vv Ww Xx Yy Zz`

Important: `Ьь are Yanalif letters`, despite their Unicode Cyrillic classification.
When the source is Yanalif or Zamanalif, write `description` in that same exact
variant. Do not translate or modernize a Latin-script Tatar description into Cyrillic.

### Markdown formatted Examples of input:
- Tatar Cyrillic(tt-Cyrl): # ТАТАРСТАН РЕСПУБЛИКАСЫ КОНСТИТУЦИЯСЕ\n(2002 елның 19 апрелендәге 1380 номерлы, 2003 елның 15 сентябрендәге 34-ТРЗ номерлы, 2004 елның 12 мартындагы 10-ТРЗ номерлы, 2005 елның 14 мартындагы 55-ТРЗ номерлы, 2010 елның 30 мартындагы 10-ТРЗ номерлы, 2010 елның 22 ноябрендәге 79-ТРЗ номерлы, 2012 елның 22 июнендәге 40-ТРЗ номерлы Татарстан Республикасы законнары редакциясендә)\n\nӘлеге Конституция, Татарстан Республикасының күпмилләтле халкы һәм татар халкы ихтыярын чагылдырып, \nкеше һәм граждан хокукларының һәм ирекләренең өстенлеген гамәлгә ашыра, халыкларның гомумтанылган үзбилгеләнү хокукына, аларның тигез хокуклылыгы, ихтыяр белдерүнең иреклелеге һәм бәйсезлеге принципларына нигезләнә,\nтарихи, милли һәм рухи традицияләрнең, мәдәниятләрнең, телләрнең сакланып калуына һәм үсешенә, гражданнар татулыгын һәм милләтара килешүне тәэмин итүгә ярдәм итә, \nфедерализм принципларында демократиянең ныгуы, Татарстан Республикасының социаль-икътисадый үсеше, Россия Федерациясе халыкларының тарихи барлыкка килгән бердәмлеген саклап калу өчен шартлар тудыра.\n\n## I КИСӘК. КОНСТИТУЦИЯЧЕЛ КОРЫЛЫШ НИГЕЗЛӘРЕ\n### 1 статья\n1. Татарстан Республикасы – Россия Федерациясе Конституциясе, Татарстан Республикасы Конституциясе һәм «Россия Федерациясе дәүләт хакимияте органнары һәм Татарстан Республикасы дәүләт хакимияте органнары арасында эшләр бүлешү һәм үзара вәкаләтләр алмашу турында» Россия Федерациясе һәм Татарстан Республикасы Шартнамәсе нигезендә Россия Федерациясе белән берләшкән һәм Россия Федерациясе субъекты булган демократик хокукый дәүләт. Татарстан Республикасы суверенитеты, Россия Федерациясе карамагындагы мәсьәләләрдән һәм Россия Федерациясе һәм Татарстан Республикасының уртак карамагындагы мәсьәләләр буенча Россия Федерациясе вәкаләтләреннән тыш, дәүләт хакимиятенең (закон чыгару, башкарма һәм суд) бөтен тулылыгына ия булуда чагыла һәм Татарстан Республикасының аерылгысыз хасияте була.\n\n2. Татарстан Республикасы һәм Татарстан исемнәре бер үк мәгънәгә ия.\n\n3. Татарстан Республикасы статусы Татарстан Республикасының һәм Россия Федерациясенең үзара ризалыгыннан башка үзгәртелә алмый. Татарстан Республикасы чикләре аның ризалыгыннан башка үзгәртелә алмый.\x20
- Tatar Latin(Zamanalif - tt-Latn-x-zaman-alif): Tatarstan Respublikası Ministrlar Kabinetı üz ormativ-xoquqıy aktların älege Zakonğa yaraqlaştırırğa tieş.\n\nTatarstan Respublikası Prezidentı **M. Şäymiev**.\n\nQazan şähäre, 1999 yıl, 15 sentäbr. №2352.\n\n## Alfavit häm orfografiä\nOrfografiä — döres yazu qağidäläre digän süz. Ul bilgele ber alfavitqa nigezlänä. Bu orfografiä Tatarstan Respublikası Prezidentı tarafınnan 1999 yılnıñ 15 sentäbrendä qul quyılğan Zakonda qabul itelgän alfavitqa nigezlänep tözelde.\n\nYaña alfavit 34 xäreftän tora, anda suzıq awazlarnı belderüçe — 9, tartıqlarnı belderüçe — 25 xäref kürsätelgän. Apostrof, siräk qullanılğanlıqtan, alfavitta ayırım urın almağan, ul, hämzäne (tä’min) belderüçe häm neçkälek bilgese bularaq, barı orfografiädä genä isäpkä alına.\n\nBu zakondağı alfavit, nigezdä, 1927—1939 yıllarda qullanılğan “Yañalif” alfavitın yañadan torğızuğa qaytıp qala. Läkin biredä “Yañalif”ne tulısınça şul kileş kire qaytaru yuq, häm ul bula da almıy, çönki anıñ qullanılmawına 60 yıl ütte, tormış üzgärde: yazuları latin grafikasına nigezlängän Könbatış tellären öyränü massaküläm küreneşkä äylände, xalıqara urtaq kompyuterlar belän eş itü, xätta dönyaküläm informatsiä sistemasına — internetqa çığu ğädätkä kerde, törki xalıqlarnıñ üzara aralaşa, ber-bersen ruxi bayıta alu mömkinlekläre açıldı.\n\nMenä şul şartlarda “Yañalif” üzgärtelmiçä torğızılğan bulsa, tatar balası, tatar häm çit il latinitsaları arasındağı ayırmalarnı kübräk kürep, qıyın xäldä yışraq qalır ide, tatar keşese, kompyuter qullanğanda, bigräk tä anıñ yärdämendä internetqa çığıp eşlägändä, qıyınlıqlarnı kübräk kürer ide, törki tuğannarınıñ yazuların uqırğa turı kilsä dä, törle çitenleklärgä duçar bulır ide.\n\nŞuşı äytelgännärne istä totıp, TR Däwlät Sovetı Zakonğa “Yañalif”ne beraz üzgärtep tözelgän yaña alfavitnı täqdim itte, häm ul, bilgele, kimçelekläre bulsa da, xäzerge zaman taläplärenä nığraq cawap birä.\n\n## Tatar orfografiäsen tözü prinsipları\nHärber telneñ orfografiäse törle prinsiplarğa nigezlänep tözelä. Tatar orfografiäse tübändäge prinsiplarğa nigezlängän.\n\n**Fonetik prinsip** — işetelgänçä yazu digän süz. Tatarnıñ töp süzläre häm tatarça äyteleşkä buysınğan yäki turı kilgän alınmalar işetelgänçä, yäğni fonetik prinsipqa nigezlänep yazılalar: äni, ulım, öydägelär, kürşe awıllarda, büränä, säläm, kitap, namaz, magazin h.b.\n\n**Grafik prinsip** — alınma süzlärne birüçe teldägegä oxşatıp yazu digän süz. Tatarça äyteleşkä buysınıp citmägän alınma süzlär, grafik prinsipqa nigezlänep, birgän teldäge yazılışqa oxşatıp yazılalar: tarixi (tarixıy tügel), Talip (Talıyp tügel); morfologiä (marfologiä tügel), motor (mator tügel), traktor (traktır tügel) h.b.\n\n***İskärmä.*** Tarixi, Talip kebek süzlärdä i yazu (ıy yazmaw) misalında bez ekonomiä prinsibın da küzätäbez [Ekonomiä prinsibınıñ 3-nçe punktın qarağız].\n\n**Morfologik prinsip** — söylämdä üzgäreşkä oçrağan morfemanı yazuda üzgäreşsez qaldıru: [umber, umbiş] digändä un morfeması [b] awazı tä’sirendä üzgärä, läkin ul üzgäreş yazuda kürsätelmi, un morfeması saqlana: un ber, un biş, yaz — yazsa (yassa tügel), süzçän (süsçän tügel), rusça (ruçça tügel), irtänge (irtäñge tügel), isänme (isämme tügel) h.b.\n\n**Ekonomiyä prinsibı** — yazu protsessında waqıtqa häm urınğa ekonomiä yasaw öçen, süzlärne qısqartıp yazu digän süz. Bu prinsip şaqtıy küp küzätelä.\n\n1. Teldä yış qullanıla häm küplärgä tanış quşma atamalar andağı süzlärneñ baş xäreflären genä yazu yulı belän qısqartılalar: Berläşkän millätlär oyışması — BMO; Tatarstan Respublikası Ministrlar Kabinetı — TR MK; Tatarstan Fännär akademiäse — TFA; Tel, ädäbiyat häm sänğät institutı — TÄhSİ.\n\nYış oçrıy torğan ike süz qısqartılğanda, ul süzlärneñ berençe (yul) xärefläre genä noqta quyılıp yazıla: häm başqalar — h.b.; häm başqa şundıylar — h.b.ş.\n\nKüplärgä tanış bulmağan atamalarnı qısqartıp yazarğa kiräk bulğanda, ayırım tekstlarda ul atama başta tulısınça yazıla, şunda uq cäyälär eçendä anıñ qısqartılması birelä, ul tekstta annan soñ barı qısqartılma süz genä yazıla, mäsälän, Min bu mäqälämdä Tatarstan Respublikasınıñ Ekologiä institutı (TR Eİ) turında söylärgä cıyınam,— dip kürsätkännän soñ, avtor bu süzlär tezmäsen yañadan tulısınça yazmıy, anı barı TR Eİ dip kenä qısqartıp birä.\n\n2. Quşma atamalardağı yä ber süzneñ, yä barlıq süzlärneñ dä yä ike xärefe, yä ber icege yazıla: KamAZ, AlAZ, YuXİDİ, KamGes, univermag h.b. Quşma süzneñ soñğısı tulı kileş, baştağıları qısqartılıp yazılırğa da mömkin: dramtügäräk, Tatpotrebsoyuz, Kazjilstroy h.b.\n\n3. Ekonomiä prinsibı yarımäyteleşle awazlarnı yazuda kürsätmäwdä dä çağıla, mäsälän, su süzendä ike awaz arasında ı işetelgän kebek bula, läkin ul yazuda kürsätelmi (sıu dip yazılmıy); uqı, tuqı süzlärenä [u] awazı quşılğaç, [u] aldınnan [ı] işetelgän kebek bula, läkin ul yazuda kürsätelmi, uqıu dip yazılmıy, uqu dip yazıla; baru, kilü kebek fiğellärdä, tartım quşımçası aldınnan [w] işetelğän kebek bula [baruwı, kilüwe], läkin ul yazuda çağılmıy, baruı, kilüe räweşendä genä yazıla; iä, iäk, orfografiä kebek süzlärdä, [i] häm [ä] awazları arasında [y] işetelgän kebek bulsa da, anı, ekonomiä prinsibınnan çığıp, yazuda kürsätmilär. (Tağın 31-nçe §nıñ 2-nçe iskärmäsen häm 33-nçe §nıñ 2-nçe iskärmäsen qarağiz).\n\n**Tarixi prinsip** — başqaçaraq işetelsä dä, süzlärne elekke çordağıça yazu digän süz. Bu prinsip iske yazulı tellärdä (mäs., ingliz telendä) yış küzätelä, tatar telendä yuq däräcäsendä az. [o], [ö] awazları berençe icektä genä tügel, ikençe, öçençelärendä äytelsä dä, alarnı barı berençe icektä genä yazarğa digän qağidä elektän “Yañalif” orfografiäsennän küçerelde, dimäk, anıñ yazılışı, bilgele däräcädä, tarixi prinsipqa nigezlängän.\n\n## Döres yazu qağidäläre\n\n### Suzıq awaz xärefläreneñ yazılışı\n\n§ 1. A xärefe [a] awazı äytelgän här urında yazıla: ağaç, qara, kamzul, garmun h.b.\x20
- Tatar Latin(Yanalif - tt-Latn-x-yanalif): Quzƣal, ujan, ləƣnət itelgən\nQollar həm aclar dɵnjasь,\nDoşmannan yc alsьn tygelgən\nYksezlər, tollar kyz jəşe\nQanlь suƣьşqa ʙez çьƣarʙьz,\nÇimererʙez iske dɵnjanь!\nAnьꞑ urьnьna ʙez qorьrʙьz,\nTьzerʙez matur, jaꞑanь!\n\nBu ʙulьr iꞑ axьrƣь, iꞑ qatь zur çihat,\nBulьr həm ʙəjlnlmilər ʙəni insan azat!\n\nBezne hic kem azat itə almas,\nItsək — itərʙez yzeʙez,\nBezne hic kem şat itə almas,\nItsək — itərʙez yzeʙez,\nƏjdnə zalimnərgə ʙez qarşь\nƢəjrət ʙelən suƣьşьp ʙarьjq,\nTusьn ʙalqьp irek qojaşь,\nXoquqlarьʙьnь alьjq!\n\nBu ʙulьr iꞑ axьrƣь, iꞑ qatь zur çihat,\nBulьr həm ʙəjlnlmilər ʙəni insan azat!\n\nBez ʙar çihan eşceləreʙez,\nBez ʙar dɵnjanьꞑ ƣəskər,\nÇirlər ʙezneꞑ yz çirləreʙez,\nBeznęder ʙar dəylətləre!\nCьƣьjq ʙez məjdanьna ʙez,\nDoşmannar xur ʙelən qacar,\nCьƣaʙьz həm inanaʙьz\nQojaş ʙezgə nurьn cəcər!\n\nBu ʙulьr iꞑ axьrƣь, iꞑ qatь zur çihat,\nBulьr həm ʙəjlnlmilər ʙəni insan azat! Barlıq keşelər də azat həm üz abruyları həm xoquqları yağınnan tiꞑ bulıp tualar. Alarğa aqıl həm wɵcdan birelgən həm ber-bersenə qarata tuğannarça mɵnasəbəttə bulırğa tieşlər.\x20\x20
- Tatar Arabic(tt-Arab): کتاب\n،هیچده کوڭلم آچلماسلق اچم پوشسه\n،اوز اوزمنی کوره‌لمیچه روحم توشسه\nجفا چیکسه‌م، جوده‌ب بتسه‌م بو باشمنی\n،قویالمیچه جانغه جلی هیچ بر توشکه\n،حسرت صوڭره حسرت کیلب آلماش، آلماش\n،کوڭلسز اوی بله‌ن تمام ئه‌یله‌نسه باش\nکوزلرمده کیببده جیتمگان بولسه\n.حاضرگنه صغلوب، صغلوب جلاغان یاش\nشول وقتده مین قولیمه کتاب آلام\nآنڭ ایزگی صحیفه‌لرن آقتارام\n.راحتله‌نوب کیته شونده جانم، ته‌نم\n.شوندنغنه دردلريمه درمان طابام\n،اوقوب بارغان هربر یولم، هربر سوزم\nبولا مینم یول کورسه‌تکوچی یولدزم\nسویمی باشلیم بو دنیانڭ واقلقلرن\n.آچیلا‌در، نورلانا‌در کوڭلم، کوزم\nجیڭلله‌نه‌م، معصومله‌نه‌م مین شول چاقده\nرحمت ئه‌یته‌م اوقوغانم شول کتابقه\n،اشانچم آرطه‌ مینم اوز اوزیمه\n.امید برلن قاری باشلیم بولاچقغه\nاوز اوزیمه\n،تلیم بولورغه مین انسان علی\n.تلی کوڭلم تعالی بالتوالی\nکوڭلم برلن سویه‌م بختن تاتارنڭ\nکوررگه جانلیلق وقتن تاتارنڭ\n.تاتار بختی اوچون مین جان آتارمن\n.تاتار بیت مین اوزمده چن تاتارمن\n،حسابسز کوب مینم ملتکه وعده م\n.قرلماسمی واوی، والله اعلم\n
""".strip()



__all__ = [
    "DEFINE_META_PROMPT_PDF_HEADER",
    "DEFINE_META_PROMPT_NON_PDF_HEADER",
    "DEFINE_META_PROMPT_BODY",
    "DEFINE_META_PROMPT_TT_FOOTER",
]
