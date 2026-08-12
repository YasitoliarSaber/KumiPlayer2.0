from app.scrape.certification import extract_certification
from app.scrape.nfo import generate_tvshow_nfo


def test_tv_certification_uses_configured_region_fallback():
    detail = {
        "content_ratings": {
            "results": [
                {"iso_3166_1": "US", "rating": "TV-14"},
                {"iso_3166_1": "TW", "rating": "輔12級"},
            ]
        }
    }

    result = extract_certification(detail, "tv", ["CN", "TW", "US"])

    assert result.value == "輔12級"
    assert result.country == "TW"


def test_movie_certification_skips_empty_values_and_prefers_theatrical():
    detail = {
        "release_dates": {
            "results": [
                {"iso_3166_1": "CN", "release_dates": [{"certification": "", "type": 3}]},
                {
                    "iso_3166_1": "US",
                    "release_dates": [
                        {"certification": "PG-13", "type": 4},
                        {"certification": "PG", "type": 3},
                    ],
                },
            ]
        }
    }

    result = extract_certification(detail, "movie", ["CN", "US"])

    assert result.value == "PG"
    assert result.country == "US"


def test_nfo_persists_certification_and_country():
    nfo = generate_tvshow_nfo(
        title="作品A", certification="輔12級", certification_country="TW",
    )

    assert "<mpaa>輔12級</mpaa>" in nfo
    assert "<certificationcountry>TW</certificationcountry>" in nfo
