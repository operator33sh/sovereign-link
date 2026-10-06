---
name: twitter-vision
description: Verwerkt een X/Twitter-link — haalt tweet-tekst op via de oEmbed API en media via fxtwitter. Analyseert afbeeldingen/memes met Vision als die aanwezig zijn.
version: 2.0.0
tools:
  - http_request
  - vision_analyze
---

# Twitter/X Analyzer

Gebruik deze workflow **altijd** wanneer de gebruiker een x.com- of twitter.com-link stuurt.

> **Let op:** X/Twitter blokkeert headless browsers actief. Gebruik **geen** `browser_navigate` of `browser_screenshot` voor Twitter-links — deze leveren een wit scherm op. Gebruik in plaats daarvan de onderstaande API-aanpak.

## Stap 1 — Tweet-tekst ophalen via oEmbed

```
http_request(
  method="GET",
  url="https://publish.twitter.com/oembed?url=<de volledige tweet-URL>&omit_script=true"
)
```

- Dit is een publieke API van Twitter zelf — geen login vereist.
- De response is JSON. Relevante velden:
  - `html` — HTML-embed met de tweet-tekst (parse de zichtbare tekst eruit)
  - `author_name` — naam van de auteur
  - `url` — canonical URL
- Sla de tekst op als `tweet_tekst` en de auteur als `tweet_auteur`.
- Als de request mislukt (bijv. 404 of rate limit): ga door naar stap 2 voor tekst via fxtwitter.

## Stap 2 — Media en volledige tweet-data via fxtwitter

Extraheer de tweet-ID uit de URL (het laatste getal, bijv. `https://x.com/user/status/1234567890` → ID = `1234567890`).

```
http_request(
  method="GET",
  url="https://api.fxtwitter.com/status/<tweet_id>"
)
```

- Response is JSON. Relevante velden:
  - `tweet.text` — volledige tweet-tekst
  - `tweet.author.name` en `tweet.author.screen_name`
  - `tweet.media.photos` — lijst van foto-objecten met `url`
  - `tweet.media.videos` — lijst van video-objecten
  - `tweet.quote` — geciteerde tweet (recursief zelfde structuur)
- Als er foto's aanwezig zijn (`tweet.media.photos` niet leeg): ga naar stap 3.
- Als er geen media is: sla tekst op en ga naar stap 4.

## Stap 3 — Visuele analyse van afbeeldingen

Voor elke foto-URL uit `tweet.media.photos`:

```
vision_analyze(
  image_source=<foto_url>,
  prompt="Dit is een afbeelding bij een tweet. Lees alle zichtbare tekst (OCR). Beschrijf wat er op de afbeelding staat. Als het een meme is, leg dan de humor of culturele context uit. Geef een volledig beeld."
)
```

- Analyseer maximaal 3 afbeeldingen.
- Sla de analyses op als lijst `media_analyses`.

## Stap 4 — Eindantwoord samenstellen

Combineer de resultaten tot een helder antwoord:

1. **Auteur** — naam en @handle
2. **Tweet-tekst** — de volledige tekst van de tweet
3. **Visuele inhoud** — wat er op de afbeelding(en) of meme(s) te zien is (als aanwezig)
4. **Context** — hoe de afbeelding aansluit op de tekst; leg humor, ironie of verwijzingen uit

## Foutafhandeling

- Als zowel oEmbed als fxtwitter mislukken: meld dit aan de gebruiker en vraag om de tekst zelf te plakken of een screenshot te sturen.
- Als de tweet privé of verwijderd is: meld dit expliciet.
- Als `vision_analyze` een afbeeldings-URL niet kan laden: sla die afbeelding over en vermeld het.
