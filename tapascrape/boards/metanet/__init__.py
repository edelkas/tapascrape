"""Metanet Forums (metanetfr): data from its Forumer era (2004-2008).

Before Yuku and Tapatalk, the board was an Invision Power Board 1.x hosted by
Forumer (metanet.2.forumer.com). A 2019 Wayback Machine dump of that site
holds old post and member ids, profiles, topic descriptions, polls and some
attachment files. `dump` reads it, `parse` understands its pages, `importer`
stores them in forumer_* tables (`schema`), and `link` maps old ids to ours.
"""
