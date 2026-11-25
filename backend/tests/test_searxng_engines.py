"""The bundled SearXNG engine set, and how the search client reads a partly answering instance.

Audit queries describe removed news, so which engines receive them is a privacy choice. The
settings keep the image's default set and turn on mwmbl only. Responses here are fabricated.
"""
from pathlib import Path

import httpx
import yaml

from audit.engine import Search

SETTINGS = Path(__file__).resolve().parents[2] / 'searxng' / 'settings.yml'


def test_settings_turn_on_mwmbl_and_nothing_else():
    settings = yaml.safe_load(SETTINGS.read_text(encoding='utf-8'))
    # The default set stays; a keep_only list would silently replace it.
    assert settings['use_default_settings'] is True
    # Exactly one override. Turning on another engine sends audit queries to a new provider,
    # and turning a default off changes the evidence: either needs a decision.
    assert settings.get('engines') == [{'name': 'mwmbl', 'disabled': False}]


async def test_hits_from_one_answering_engine_reach_the_search_step():
    def route(request):
        assert request.url.params['categories'] == 'general'
        return httpx.Response(200, json={
            'results': [
                {'url': 'https://news.example/one', 'title': 'Fictional one',
                 'content': 'Fabricated snippet one.', 'engines': ['mwmbl']},
                {'url': 'https://other.example/two', 'title': 'Fictional two',
                 'content': 'Fabricated snippet two.', 'engines': ['mwmbl']},
            ],
            'unresponsive_engines': [['brave', 'too many requests'],
                                     ['duckduckgo', 'CAPTCHA'], ['startpage', 'CAPTCHA']],
        })

    search = Search('https://search.example', transport=httpx.MockTransport(route))
    results, failed = await search.search_checked('fictional query')
    assert not failed
    assert [r.url for r in results] == ['https://news.example/one', 'https://other.example/two']
    assert search.state == 'ok'
