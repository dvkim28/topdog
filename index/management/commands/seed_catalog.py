"""Seed regions, providers, brands and the tracked game catalogue.

    python manage.py seed_catalog
"""

from django.core.management.base import BaseCommand
from django.utils.text import slugify

from index.models import Brand, Category, Game, GameAlias, Provider, Region

REGIONS = [
    {
        "code": "ES", "name": "Spain", "regulator": "DGOJ", "tld": ".es", "currency": "EUR",
        "help_line": "FEJAR 900 200 225",
        "legal_notice_en": "Data monitored strictly from legal Spanish domain operators (.es) holding "
                           "Directorate General for the Regulation of Gambling (DGOJ) licences. "
                           "Must be 18+ to play (Juego Seguro / Jugar Bien).",
        "legal_notice_es": "Datos monitorizados exclusivamente de operadores con dominio legal espanol (.es) "
                           "con licencia de la Direccion General de Ordenacion del Juego (DGOJ). "
                           "Prohibido el juego a menores de 18 anos (Juego Seguro / Jugar Bien).",
    },
    {
        "code": "MX", "name": "Mexico", "regulator": "SEGOB", "tld": ".mx", "currency": "MXN",
        "help_line": "Linea de la Vida 800 911 2000",
        "legal_notice_en": "Operators permitted under a SEGOB gaming permit. 18+ only.",
    },
    {
        "code": "IT", "name": "Italy", "regulator": "ADM", "tld": ".it", "currency": "EUR",
        "help_line": "Numero Verde 800 558822",
        "legal_notice_en": "Operators holding an ADM (ex-AAMS) concession. 18+ only.",
    },
]

BRANDS = [
    # (name, short, domain, group, region)
    ("888casino.es", "888", "888casino.es", "888 Holdings", "ES"),
    ("Bet365.es", "B365", "casino.bet365.es", "Hillside", "ES"),
    ("Casino Barcelona", "CBCN", "casinobarcelona.es", "Grup Peralada", "ES"),
    ("Codere.es", "CODE", "codere.es", "Codere Online", "ES"),
    ("PlayUZU", "UZU", "playuzu.es", "Bethard Group", "ES"),
    ("LeoVegas.es", "LEO", "leovegas.es", "MGM Resorts", "ES"),
    ("Sportium", "SPO", "sportium.es", "Cirsa", "ES"),
    ("William Hill.es", "WH", "williamhill.es", "evoke plc", "ES"),
    ("Bwin.es", "BWIN", "bwin.es", "Entain", "ES"),
    ("Luckia.es", "LUCK", "luckia.es", "Luckia Gaming", "ES"),
    ("PokerStars Casino.es", "PSC", "pokerstarscasino.es", "Flutter", "ES"),
    ("Versus.es", "VERS", "versus.es", "Comar", "ES"),
    ("Paf.es", "PAF", "paf.es", "Paf", "ES"),
    ("Marca Apuestas", "MARC", "marcaapuestas.es", "Cirsa", "ES"),
    ("Botemania.es", "BOTE", "botemania.es", "Gamesys / Bally's", "ES"),
    ("Codere.mx", "CDMX", "codere.mx", "Codere Online", "MX"),
    ("Caliente.mx", "CAL", "caliente.mx", "Grupo Caliente", "MX"),
    ("Strendus", "STR", "strendus.com.mx", "Logrand", "MX"),
    ("Snai.it", "SNAI", "snai.it", "Snaitech", "IT"),
    ("Sisal.it", "SIS", "sisal.it", "Flutter", "IT"),
]

GAMES = [
    ("Ruleta en Vivo", "Evolution", Category.LIVE_ROULETTE, True,
     ["Ruleta en Directo", "Spanish Roulette Live", "Ruleta Espanola en Vivo"]),
    ("Gates of Olympus", "Pragmatic Play", Category.CLASSIC_SLOT, False,
     ["Gates of Olympus 1000", "Puertas del Olimpo"]),
    ("Lightning Roulette", "Evolution", Category.LIVE_ROULETTE, False,
     ["Ruleta Lightning", "Lightning Roulette Live"]),
    ("Chiquito", "MGA Games", Category.CLASSIC_SLOT, True, ["El Chiquito", "Chiquito Slot"]),
    ("Aviator", "Spribe", Category.CRASH, False, ["Aviator Crash", "El Aviador"]),
    ("Crazy Time", "Evolution", Category.GAME_SHOW, False, ["Crazy Time Live"]),
    ("La Mina de Oro", "MGA Games", Category.CLASSIC_SLOT, True, ["Mina de Oro"]),
    ("Bonanza Megaways", "Big Time Gaming", Category.MEGAWAYS, False, ["Bonanza"]),
    ("Blackjack Espanol en Vivo", "Playtech", Category.LIVE_BLACKJACK, True,
     ["Blackjack en Espanol", "Spanish Blackjack Live"]),
    ("Sweet Bonanza", "Pragmatic Play", Category.CLASSIC_SLOT, False, ["Sweet Bonanza 1000"]),
    ("Starburst", "NetEnt", Category.CLASSIC_SLOT, False, ["Starburst XXXtreme"]),
    ("Ruleta Automatica", "Playtech", Category.LIVE_ROULETTE, True, ["Auto Roulette"]),
    ("Big Bass Bonanza", "Pragmatic Play", Category.CLASSIC_SLOT, False, ["Bigger Bass Bonanza"]),
    ("JetX", "SmartSoft", Category.CRASH, False, ["Jet X"]),
    ("Blackjack Multihand", "Evolution", Category.TABLE, False, ["Multihand Blackjack"]),
    ("Book of Dead", "Play'n GO", Category.CLASSIC_SLOT, False, ["Libro de los Muertos"]),
    ("Monopoly Live", "Evolution", Category.GAME_SHOW, False, ["Monopoly en Vivo"]),
    ("Divine Fortune Megaways", "NetEnt", Category.MEGAWAYS, False, ["Divine Fortune"]),
]


class Command(BaseCommand):
    help = "Seed regions, brands, providers and the tracked game catalogue."

    def handle(self, *args, **options):
        for payload in REGIONS:
            Region.objects.update_or_create(code=payload["code"], defaults=payload)
        self.stdout.write(self.style.SUCCESS(f"{len(REGIONS)} regions ready"))

        regions = {r.code: r for r in Region.objects.all()}
        for name, short, domain, group, region_code in BRANDS:
            Brand.objects.update_or_create(
                slug=slugify(name),
                defaults={
                    "name": name,
                    "short_code": short,
                    "domain": domain,
                    "homepage_url": f"https://www.{domain}/",
                    "operator_group": group,
                    "region": regions[region_code],
                    "status": Brand.Status.ACTIVE,
                },
            )
        self.stdout.write(self.style.SUCCESS(f"{len(BRANDS)} brands ready"))

        for title, provider_name, category, local, aliases in GAMES:
            provider, _ = Provider.objects.get_or_create(
                name=provider_name, defaults={"slug": slugify(provider_name)}
            )
            game, _ = Game.objects.update_or_create(
                slug=slugify(title),
                defaults={
                    "title": title,
                    "provider": provider,
                    "category": category,
                    "is_local_favourite": local,
                },
            )
            for alias in aliases:
                GameAlias.objects.get_or_create(game=game, text=alias)
        self.stdout.write(self.style.SUCCESS(f"{len(GAMES)} games ready"))
        self.stdout.write("Next: python manage.py backfill_history --days 45")
