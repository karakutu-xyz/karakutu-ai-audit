"""Invented benchmark inputs, generated in memory."""

from html import escape


def demo_cases():
    """Generate fictional smoke inputs with the original labels and content."""
    return [
        {
            "label": "Fictional smoke / low public interest",
            "expected_verdict": "LIKELY_LOW_PUBLIC_INTEREST",
            "removed_article": {
                "url": "https://example.invalid/synthetic-weekend-colors",
                "title": "Fictional lifestyle exercise: choosing "
                "weekend colors",
                "content": "This is an invented benchmark, not a "
                "real news article. A fictional "
                "lifestyle column offers lighthearted "
                "suggestions about matching socks "
                "with a favorite sweater and choosing "
                "flowers for a table. Readers are "
                "encouraged to pick the colors they "
                "personally enjoy. There is no "
                "allegation, public authority, "
                "policy, safety issue or political "
                "event. The column closes with a "
                "playful suggestion to try a blue "
                "scarf next weekend.",
            },
        },
        {
            "label": "Fictional smoke / public-interest investigation",
            "removed_article": {
                "url": "https://example.invalid/synthetic-procurement-investigation",
                "title": "Council opens inquiry into missing "
                "water-treatment procurement records",
                "content": "The municipal council has opened an "
                "inquiry into alleged conflicts of "
                "interest in its water-treatment "
                "procurement process. Inspectors "
                "requested tender records after "
                "finding payments for work without "
                "completion certificates. The "
                "procurement committee has suspended "
                "new payments pending independent "
                "review. Residents have asked the "
                "council to publish the contract and "
                "the water-quality monitoring "
                "reports. Council members say the "
                "inquiry will examine public "
                "spending, contractor selection and "
                "whether inspection requirements were "
                "followed. The council has asked all "
                "departments to preserve relevant "
                "records.",
            },
        },
    ]


INVENTED = "Bu metin bir test için uydurulmuştur; gerçek bir haber değildir."


def _archive_html(title, paragraphs):
    """Put archive furniture before the prose, beyond the relevance window."""
    head = (
        '<html lang="tr"><head><title>' + escape(title) + "</title>"
        '<script>const bolumler = ["yerel", "kultur", "yasam"];</script>'
        "<style>nav a { display: inline-block; padding: 8px; }"
        "article { max-width: 70ch; margin: auto; }</style></head><body>"
    )
    menu = "".join(
        f'<a href="/bolum/{i}">Uydurma bülten bölüm menüsü {i}</a>'
        for i in range(55)
    )
    return (
        head
        + "<nav>"
        + menu
        + "</nav><article><h1>"
        + escape(title)
        + "</h1>"
        + "".join("<p>" + escape(p) + "</p>" for p in paragraphs)
        + "</article><footer>Uydurma test sayfası sonu.</footer></body></html>"
    )


def verdict_quality_cases():
    """Fifteen Turkish cases measuring relevance boundaries and archive HTML."""
    stories = [
        (
            "public-affairs-no-allegation",
            True,
            "dil-haklari",
            "Bir partiden azınlık dili ve kimliği için anayasal güvence çağrısı",
            "Bir parti, kurmaca Ovaca dilini konuşan azınlığın dil ve kimlik "
            "haklarının anayasada açıkça tanınmasını istedi. Açıklamada, kamu "
            "kurumlarına anadilinde başvuru ve kültürel faaliyetlere eşit erişim "
            "önerildi. Parti sözcüsü, önerinin gelecek yasama döneminde bütün "
            "gruplarla görüşüleceğini bildirdi.",
            "Hazırlanan metin, dil öğretimi için yerel kurslara kaynak ayrılmasını "
            "ve kimlik beyanının kişilerin tercihine bırakılmasını içeriyor. "
            "Toplantıya katılan dernek temsilcileri teklifin kapsamını sordu. "
            "Parti, anayasa komisyonuna sunacağı taslağı gelecek ay kamuya "
            "açacağını duyurdu; açıklamada herhangi bir suçlama yer almadı.",
        ),
        (
            "public-affairs-no-allegation",
            True,
            "belediye-butcesi",
            "Yeşilova meclisi bütçeyi ve su tarifesinin endekslenmesini kabul etti",
            "Yeşilova belediye meclisi gelecek yılın bütçesini oy çokluğuyla "
            "kabul etti. Karara göre su tarifesi her üç ayda bir açıklanan "
            "enflasyon oranına göre güncellenecek. Belediye başkanı, bakım "
            "giderlerinin bu yöntemle karşılanacağını ve düşük gelirli "
            "hanelere indirimli tarifenin süreceğini söyledi.",
            "Muhalefet grubu, tarife hesabının her dönem meclis gündemine "
            "getirilmesini önerdi. Kabul edilen bütçede yeni su deposu, "
            "mahalle yollarının bakımı ve kütüphane giderleri için ayrı "
            "kalemler bulunuyor. Tutanaklar belediyenin duyuru panosuna "
            "asılacak. Haberde usulsüzlük iddiası değil, bütün aboneleri "
            "ilgilendiren bütçe ve fiyatlandırma kararı aktarılıyor.",
        ),
        (
            "public-affairs-no-allegation",
            True,
            "secim-baraji",
            "Meclis seçim barajını değiştiren teklifi kabul etti",
            "Kurmaca ülkenin parlamentosu, genel seçimlerde uygulanan ülke "
            "barajını yüzde yediden yüzde beşe indiren teklifi kabul etti. "
            "Oylama açık usulle yapıldı ve sonuç birleşim tutanağına geçirildi. "
            "Komisyon sözcüsü yeni oranın bir sonraki genel seçimde "
            "uygulanacağını, devam eden yerel takvimi etkilemeyeceğini belirtti.",
            "Bir muhalefet milletvekili küçük partilerin temsil olanağının "
            "genişleyeceğini savundu. Diğer grup, seçim kurullarının seçmen "
            "bilgilendirme çalışmalarını erken başlatmasını istedi. Düzenleme "
            "aday başvuru tarihlerini değiştirmiyor. Kabul edilen maddeler ve "
            "oy dağılımı meclisin haftalık bülteninde yayımlanacak; görüşmelerde "
            "bir yolsuzluk veya kişisel kusur iddiası ileri sürülmedi.",
        ),
        (
            "public-affairs-no-allegation",
            True,
            "hastane-birlesmesi",
            "Bakanlık Karaçam ilçesindeki devlet hastanesini kapatma kararı aldı",
            "Sağlık bakanlığı, Karaçam ilçesindeki devlet hastanesinin hizmetlerini "
            "bölge hastanesiyle birleştireceğini açıkladı. İlçedeki bina yaz "
            "sonunda kapanacak; poliklinik randevuları yeni merkezden verilecek. "
            "Bakanlık, personelin aynı kadrolarla bölge hastanesine aktarılacağını "
            "ve ambulans biriminin ilçede kalacağını bildirdi.",
            "Belediye meclisi, yaşlı hastaların ulaşımı için servis saatlerinin "
            "artırılmasını talep etti. İlçe sakinleri diyaliz ve çocuk sağlığı "
            "hizmetlerinin hangi günlerde sunulacağını sordu. Bakanlık, geçiş "
            "takvimi ile hasta yönlendirme çizelgesinin aile sağlığı merkezlerine "
            "gönderileceğini duyurdu. Haber, kamu hizmetinin yerini ve erişim "
            "koşullarını değiştiren kararı anlatıyor; suçlama içermiyor.",
        ),
        (
            "accountability-control",
            True,
            "aritma-ihalesi",
            "Belediyenin su arıtma ihalesi hakkında inceleme açıldı",
            "Yeşilova belediyesinin su arıtma tesisi ihalesinde teslim edilmeyen "
            "cihazlar için ödeme yapıldığı iddiasıyla inceleme başlatıldı. "
            "Denetçiler kabul belgeleri ile depo kayıtlarının birbirini "
            "tutmadığını bildirdi. Meclis denetim komisyonu sözleşmenin ve "
            "ödeme çizelgesinin korunmasını istedi.",
            "İhale birimi, bütün belgeleri inceleme heyetine vereceğini açıkladı. "
            "Bir muhalefet üyesi, arıtma kapasitesi düşerken ödeme yapılmasının "
            "kamu kaynağının kullanımı açısından açıklanması gerektiğini söyledi. "
            "Yüklenici eksik parçaların yolda olduğunu savundu. İnceleme henüz "
            "sonuçlanmadı ve kimse hakkında kesin karar verilmedi. Komisyon, "
            "tesiste bağımsız sayım yapılmasını ve sonuçların abonelerle "
            "paylaşılmasını talep etti.",
        ),
        (
            "accountability-control",
            True,
            "iscilerin-yuruyusu",
            "Barışçıl işçi yürüyüşüne polis müdahalesi için barodan şikâyet",
            "Karaçam ilçesinde ücret alacakları için sessiz yürüyüş yapan "
            "işçilere polis güç kullandı. Baro gözlemcileri, grubun yolu "
            "kapatmadığını ve dağılma çağrısından hemen sonra kalkanlarla "
            "itildiğini bildirdi. Yaralanan iki işçi sağlık merkezine götürüldü. "
            "Baro, müdahalenin görüntüleriyle birlikte savcılığa başvurdu.",
            "Başvuruda toplantı hakkının ve güç kullanımındaki ölçülülüğün "
            "incelenmesi istendi. Emniyet birimi olay tutanağını ilgili "
            "makamlara ileteceğini duyurdu. İşçi temsilcileri yürüyüşün "
            "önceden ilan edildiğini söyledi. Savcılık henüz bir sonuç "
            "açıklamadı. Baro, sağlık raporlarının dosyaya eklenmesini ve "
            "alandaki kamera kayıtlarının silinmeden korunmasını talep etti.",
        ),
        (
            "accountability-control",
            True,
            "gosteri-yasagi",
            "Valilik bütün ilde on beş gün gösteri yasağı ilan etti",
            "Kurmaca Yeşilova ilinin valiliği, açık alanlardaki bütün toplantı "
            "ve gösterileri on beş gün süreyle yasakladı. Duyuruda genel "
            "güvenlik gerekçesine yer verildi, ancak belirli bir etkinlik "
            "veya somut risk açıklanmadı. Yasak merkezle birlikte bütün "
            "ilçeleri ve önceden bildirilmiş yürüyüşleri kapsıyor.",
            "Meslek örgütleri, kararın barışçıl toplantı hakkını topluca "
            "sınırladığını belirterek idare mahkemesinde dava açacaklarını "
            "bildirdi. Bir sendika, kapalı salon toplantısının da iptal "
            "edilip edilmeyeceğini sordu. Valilik uygulama ayrıntılarının "
            "ilçe birimlerine gönderildiğini açıkladı. Avukatlar, kararın "
            "gerekçesi ve süresinin yargı denetimine açık olması gerektiğini "
            "vurguladı; başvurular henüz karara bağlanmadı.",
        ),
        (
            "private-matter-influential",
            False,
            "babalik-davasi",
            "Varlıklı bir iş insanına karşı özel babalık davası açıldı",
            "Varlıklı bir iş insanına karşı aile mahkemesinde babalık "
            "iddiasıyla dava açıldı. Başvuru, iki yetişkin arasındaki eski "
            "bir ilişkinin ardından çocuğun soybağının belirlenmesini "
            "konu alıyor. Tarafların avukatları özel hayata ilişkin "
            "ayrıntıları paylaşmadı; dosyanın kapalı incelenmesi istendi.",
            "İş insanının şirketleri, kamu görevleri veya kamu kaynakları "
            "davaya konu değil. Haber yalnızca özel aile uyuşmazlığı ve "
            "duruşmanın ertelenmesiyle ilgili. Avukatlar mahkemenin bir "
            "sonraki oturum için belge beklediğini söyledi. Herhangi bir "
            "şiddet, baskı ya da görevin kötüye kullanılması iddiası "
            "bulunmuyor. Başvurunun esası hakkında karar verilmedi ve "
            "taraflar aile yaşamlarının gizliliğine saygı gösterilmesini istedi.",
        ),
        (
            "private-matter-influential",
            False,
            "vekilin-dugunu",
            "Bir milletvekilinin düğününde bahçe düzeni ve konuk listesi",
            "Bir milletvekili, özel bir kır bahçesinde yapılan törenle "
            "evlendi. Haberde masaların yerleşimi, çiçeklerin rengi ve "
            "yakın arkadaşlardan oluşan konuk listesi anlatılıyor. "
            "Davetlilere yöresel tatlılar sunuldu ve tören aile "
            "albümü için çekilen fotoğrafların ardından sona erdi.",
            "Mekânın giderleri çift tarafından karşılandı. Kamu aracı, "
            "kurum bütçesi veya resmî personel kullanılmadı. Konuşmalar "
            "çiftin tanışma anılarıyla sınırlı kaldı; siyasi bir açıklama "
            "yapılmadı. Davetliler oturma düzeni ile müzik seçimi hakkında "
            "kısa yorumlar paylaştı. Yazı, bir kamu kararı ya da görev "
            "sırasındaki davranış yerine kişisel kutlamanın ayrıntılarını "
            "aktarıyor. Törenin ertesi gününde aile içinde küçük bir "
            "kahvaltı düzenlendiği de belirtildi.",
        ),
        (
            "private-matter-influential",
            False,
            "mac-tartismasi",
            "Bir bakanın yetişkin çocuğu maç yorumları üzerine tartıştı",
            "Bir bakanın yetişkin çocuğu, kişisel sosyal medya hesabında "
            "kurmaca iki futbol takımının maçı hakkında tartıştı. "
            "Tartışma son dakikadaki oyuncu değişikliği ve kalecinin "
            "performansı üzerine başladı. Paylaşımlarda arkadaşlar "
            "birbirine maç istatistikleri ve eski tribün fotoğrafları gönderdi.",
            "Hesabın sahibi kamu görevlisi değil ve bakanlık adına "
            "konuşmadığını belirtti. Mesajlarda tehdit, kamu yetkisinin "
            "kullanılması veya kurumlara talimat bulunmuyor. Konu "
            "yalnızca taraftarların beğenileri ve maçın sonucu. Ertesi "
            "sabah taraflar atışmayı şakayla bitirdi. Haberde transfer "
            "ödemesi, bahis veya kamu kaynağına ilişkin bir iddia "
            "yer almıyor; yazı kişisel hesabın spor sohbetini aktarıyor.",
        ),
        (
            "low-interest-control",
            False,
            "nehirde-gemi-asansoru",
            "Uzak bir ülkedeki nehir asansörünün dişli düzeni",
            "Kurmaca uzak bir ülkede, Dumanlı Nehir üzerindeki gemi "
            "asansörünün mekanik yapısını anlatan bir teknik gezi "
            "yazısı yayımlandı. Yazıda su dolu haznenin karşı ağırlıklarla "
            "yükseltilmesi ve teknelerin iki farklı seviyedeki kanallar "
            "arasında taşınması sade çizimlerle açıklanıyor.",
            "Mühendislik meraklıları için hazırlanan bölümde dişli "
            "çapları, bakım aralıkları ve kabin içindeki göstergeler "
            "tanıtılıyor. Herhangi bir kaza, kamu ihalesi veya çevre "
            "uyuşmazlığı anlatılmıyor. Metin yerel bir kararın haberini "
            "vermiyor; başka bir ülkedeki eski bir düzeneğin nasıl "
            "çalıştığını gösteriyor. Son bölüm, ziyaret sırasında "
            "maket üzerindeki hareketi izleme ve fotoğraf açısı seçme "
            "önerileriyle teknik tanıtımı tamamlıyor.",
        ),
        (
            "low-interest-control",
            False,
            "uydu-frekansi",
            "Kurmaca televizyon kanalının yeni uydu frekansı nasıl eklenir",
            "Kurmaca bir televizyon kanalı, eğlence yayınını yeni uydu "
            "frekansına taşıdı. Bu açıklayıcı yazı, kumandadan kurulum "
            "menüsünün açılmasını ve otomatik aramanın yeniden "
            "başlatılmasını adım adım anlatıyor. Kanalın alıcıdaki "
            "sıra numarası cihazın önceki listesine göre değişebiliyor.",
            "Rehberde örnek frekans alanına 12340 değerinin, simge "
            "hızı alanına 27500 değerinin yazılması isteniyor. Görüntü "
            "gelmezse kablo bağlantısı ve sinyal çubuğu kontrol ediliyor. "
            "Yazıda yayın yasağı, sansür, sahiplik değişimi veya kamu "
            "düzenlemesi bulunmuyor. Amaç yalnızca izleyicinin teknik "
            "ayarını tamamlaması. Son bölümde favori kanal listesinin "
            "kaydedilmesi ve eski numaranın listeden kaldırılması "
            "gösteriliyor; abonelik koşulları değişmiyor.",
        ),
        (
            "low-interest-control",
            False,
            "hafta-sonu-renkleri",
            "Hafta sonu kıyafetlerinde yumuşak renkleri eşleştirme önerileri",
            "Bir yaşam köşesi, hafta sonu yürüyüşünde giyilecek "
            "kıyafetlerin renklerini eşleştirmeyi öneriyor. Yazı, "
            "açık mavi bir atkıyla krem renkli hırkanın birlikte "
            "denenebileceğini ve rahat ayakkabı seçiminin kişisel "
            "zevke bırakılmasını anlatıyor. Örnekler evdeki dolap "
            "üzerinden hazırlanmış, belirli bir marka tanıtılmıyor.",
            "İkinci bölümde çantaya küçük bir renk eklemek ve yağışlı "
            "havada koyu tonları seçmek gibi hafif öneriler var. "
            "Yazar, okurların aynanın karşısında farklı eşleşmeleri "
            "deneyip beğendiklerini kullanmasını istiyor. Metin "
            "çalışma koşulları, ürün güvenliği veya kamusal bir "
            "kararla ilgili değil. Kapanışta, kullanılmayan bir "
            "fuların yeni bir bağlama biçimiyle denenmesi ve "
            "dostlarla yapılacak kahvaltıya rahat giyinilmesi öneriliyor.",
        ),
    ]
    cases = []
    for index, (pattern, relevant, slug, title, first, second) in enumerate(
        stories
    ):
        cases.append(
            {
                "label": "Fabricated / " + title,
                "pattern": pattern,
                "expected_verdict": (
                    "INCONCLUSIVE" if relevant else "LIKELY_LOW_PUBLIC_INTEREST"
                ),
                "expected_relevance": relevant,
                "removed_article": {
                    "url": f"https://dusulke-bulteni.invalid/haber/{104200 + index}/{slug}",
                    "title": title,
                    "content": INVENTED + " " + first + "\n\n" + second,
                },
            }
        )
    for index, original in enumerate((cases[1], cases[7])):
        article = original["removed_article"]
        cases.append(
            {
                **original,
                "label": original["label"] + " / arşiv HTML",
                "pattern": "html-wrapped",
                "removed_article": {
                    **article,
                    "url": (
                        f"https://dusulke-bulteni.invalid/haber/{104300 + index}/"
                        + ("butce-arsiv" if index == 0 else "babalik-arsiv")
                    ),
                    "content": _archive_html(
                        article["title"],
                        article["content"].split("\n\n"),
                    ),
                },
            }
        )
    return cases
