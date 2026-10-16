"""AI 接入使用假回應驗證，不呼叫中轉 API、不消耗額度。"""
import copy
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _runner
from osu_tourney import ai_brief as ai, render

CONFIG = ai.Config('https://relay.example/v1', 'test-model', 'test-key')
NOW = '2026-10-16T00:00:00+00:00'
RECORD = {'topic_id': 1, 'title': 'Example Cup', 'decision': 'include', 'status': 'open',
          'body_html': '<p>Rank 50K-100K. Team size 3.</p><a href="https://example.com/signup">Sign up</a>'}


def sections():
    return {key: [{'text': '隊伍為 3 人。', 'evidence': 'Team size 3.', 'status': 'supported'}]
            for key in ai.SECTIONS}


def response(value=None, status=200, finish='stop'):
    return Mock(status_code=status, json=Mock(return_value={
        'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(value or sections())}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
    }))


def test_request_uses_config_and_does_not_follow_redirects():
    with patch.object(ai.requests, 'post', return_value=response()) as post:
        result, usage = ai.generate(ai.source_text(RECORD), CONFIG)
    assert result == sections()
    assert usage['total_tokens'] == 150
    assert post.call_args.args == ('https://relay.example/v1/chat/completions',)
    assert post.call_args.kwargs['allow_redirects'] is False
    assert post.call_args.kwargs['json']['model'] == 'test-model'
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer test-key'
    assert 'test-key' not in repr(CONFIG)


def test_missing_unsupported_and_invented_quotes_are_rejected():
    for item in ({'text': '沒有排名限制', 'evidence': '', 'status': 'supported'},
                 {'text': '全球開放', 'evidence': 'Worldwide!', 'status': 'supported'},
                 {'text': '報名 https://invented.example/signup', 'evidence': 'Sign up', 'status': 'supported'},
                 {'text': '台灣可參加', 'evidence': '', 'status': 'missing'}):
        value = sections()
        value['requirements'] = [item]
        try:
            ai.validate_sections(value, ai.source_text(RECORD))
        except ai.BriefError:
            pass
        else:
            raise AssertionError('unsupported claim accepted')


def test_missing_fields_are_allowed_but_not_invented():
    value = sections()
    value['requirements'] = [{'text': '歷史最高排名：原帖未說明', 'evidence': '', 'status': 'missing'}]
    assert ai.validate_sections(value, ai.source_text(RECORD)) == value


def test_cache_survives_model_and_provider_changes():
    records = {'1': copy.deepcopy(RECORD)}
    with patch.object(ai, 'generate', return_value=(sections(), {'total_tokens': 150})) as generate:
        assert ai.update_briefs(records, [1], CONFIG, 3, NOW) == 1
        other = ai.Config('https://other.example/v1', 'other-model', 'other-key')
        assert ai.update_briefs(records, [1], other, 3, NOW) == 0
        assert generate.call_count == 1
    assert 'test-key' not in json.dumps(records)
    records['1']['body_html'] += '<p>Rules changed.</p>'
    assert ai.display_brief(records['1']) == {'stale': True}
    with patch.object(ai, 'generate', return_value=(sections(), {})) as generate:
        assert ai.update_briefs(records, [1], CONFIG, 3, NOW) == 1
        assert ai.display_brief(records['1'])['stale'] is False


def test_limits_count_calls_and_errors_keep_previous_result():
    records = {str(i): dict(RECORD, topic_id=i) for i in range(1, 5)}
    with patch.object(ai, 'generate', return_value=(sections(), {})) as generate:
        assert ai.update_briefs(records, [1, 2, 3, 4], CONFIG, 2, NOW) == 2
        assert generate.call_count == 2
    previous = copy.deepcopy(records['1']['ai_brief'])
    records['1']['title'] = 'Changed title'
    with patch.object(ai, 'generate', side_effect=ai.BriefError('failed')) as generate:
        assert ai.update_briefs(records, [1, 3, 4], CONFIG, 3, NOW) == 1
        assert generate.call_count == 1
    assert records['1']['ai_brief'] == previous


def test_long_and_image_only_sources_do_not_spend_tokens():
    records = {'1': dict(RECORD, body_html='<img src="https://example.com/banner.png">'),
               '2': dict(RECORD, topic_id=2, body_html='<p>' + 'x' * 31000 + '</p>')}
    with patch.object(ai, 'generate') as generate:
        assert ai.update_briefs(records, [1, 2], CONFIG, 3, NOW) == 0
        generate.assert_not_called()


def test_bad_responses_and_timeouts_do_not_leak_reply_or_key():
    for reply in (response(status=401), response(status=302), response(finish='length'),
                  Mock(status_code=200, json=Mock(return_value={'choices': [None]})),
                  Mock(status_code=200, headers={}, json=Mock(side_effect=ValueError('test-key')))):
        with patch.object(ai.requests, 'post', return_value=reply):
            try:
                ai.generate(ai.source_text(RECORD), CONFIG)
            except ai.BriefError as exc:
                assert 'test-key' not in str(exc)
            else:
                raise AssertionError('invalid response accepted')
    with patch.object(ai.requests, 'post', side_effect=ai.requests.Timeout('test-key')):
        try:
            ai.generate(ai.source_text(RECORD), CONFIG)
        except ai.BriefError as exc:
            assert 'test-key' not in str(exc)
        else:
            raise AssertionError('timeout accepted')


def test_non_json_diagnostics_show_endpoint_without_reply_or_key():
    reply = Mock(status_code=200, headers={'Content-Type': 'text/html; charset=utf-8'},
                 text='<html>test-key private reply</html>', json=Mock(side_effect=ValueError('test-key')))
    with patch.object(ai.requests, 'post', return_value=reply), patch.object(ai.log, 'info') as info:
        try:
            ai.generate(ai.source_text(RECORD), CONFIG)
        except ai.BriefError as exc:
            assert 'text/html' in str(exc)
            assert 'test-key' not in str(exc)
            assert 'private reply' not in str(exc)
        else:
            raise AssertionError('HTML response accepted')
    info.assert_any_call('AI 請求端點 %s；模型 %s',
                         'https://relay.example/v1/chat/completions', 'test-model')
    assert 'test-key' not in str(info.call_args_list)


def test_config_preserves_base_path_and_rejects_unsafe_urls():
    with patch.dict(os.environ, {'AI_API_KEY': 'test-key', 'AI_BASE_URL': 'https://agentrouter.org/'}, clear=True):
        config = ai.Config.from_env()
        assert config.base_url == 'https://agentrouter.org'
        with patch.object(ai.requests, 'post', return_value=response()) as post:
            ai.generate(ai.source_text(RECORD), config)
        assert post.call_args.args == ('https://agentrouter.org/chat/completions',)
        os.environ['AI_BASE_URL'] = 'https://relay.example/custom/v1/'
        assert ai.Config.from_env().base_url == 'https://relay.example/custom/v1'
        for bad in ('http://example.com/v1', 'https://key@example.com/v1', 'https://example.com?key=x'):
            os.environ['AI_BASE_URL'] = bad
            try:
                ai.Config.from_env()
            except ai.BriefError:
                pass
            else:
                raise AssertionError('unsafe URL accepted')
    with patch.dict(os.environ, {'AI_API_KEY': 'test-key'}, clear=True):
        config = ai.Config.from_env()
        assert config.base_url == 'https://agentrouter.org/v1'
        with patch.object(ai.requests, 'post', return_value=response()) as post:
            ai.generate(ai.source_text(RECORD), config)
        assert post.call_args.args == ('https://agentrouter.org/v1/chat/completions',)
    with patch.dict(os.environ, {}, clear=True):
        try:
            ai.Config.from_env()
        except ai.BriefError:
            pass
        else:
            raise AssertionError('missing credential accepted')


def test_render_keeps_owner_text_and_original_next_to_ai():
    record = copy.deepcopy(RECORD)
    record['ai_brief'] = {'source_hash': ai.source_hash(ai.source_text(record)),
                          'sections': sections(), 'generated_at': NOW}
    payload = render.build_payload({'1': record}, {}, NOW, {'1': {'summary': '站長手寫說明'}})
    row = payload['tournaments'][0]
    assert row['summary'] == '站長手寫說明'
    assert row['ai_brief']['sections'] == sections()
    assert row['post_document']
    assert row['deadline_iso'] == ''  # AI 不覆寫原本的欄位。
    html = render.render_dashboard(payload)
    assert "el('blockquote',null,item.evidence" in html
    assert "body.appendChild(brief)" in html


def test_scrape_only_analyses_visible_topics_when_explicitly_enabled():
    import test_custom_roundtrip as integration
    from osu_tourney import scrape
    with integration._sandbox() as root:
        with patch.object(scrape, 'Fetcher', return_value=scrape.OfflineSource(scrape.FIXTURES)), \
             patch.object(ai.Config, 'from_env', return_value=CONFIG), \
             patch.object(ai, 'generate', return_value=(sections(), {})) as generate:
            assert scrape.main(['--no-push', '--ai-limit', '1']) == 0
            assert generate.call_count == 1
        data = integration._data(root)['tournaments']
        assert data['2252361']['ai_brief']['sections'] == sections()


if __name__ == '__main__':
    raise SystemExit(_runner.run(globals()))
