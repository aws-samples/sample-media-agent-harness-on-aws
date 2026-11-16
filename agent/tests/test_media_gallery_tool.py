"""show_media_gallery: ONE gallery tab, labelled as given, written by the harness (no Render Agent).

Staging a5b5acaf: asked for "one audio clip-gallery labelled with voice names", the Render Agent built an
nle-timeline labelled by file id, and re-showing one voice folded into an existing tab -- four tries to hear v4.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from render.gallery import build_gallery_manifest  # noqa: E402

V4 = "https://m2cv6-x-proxy.s3.amazonaws.com/micro-workflow/tts-on-aws/voices/85bc/preview.wav?X-Amz-Signature=s"


def test_audio_items_become_a_clip_gallery_with_the_given_labels():
    m = build_gallery_manifest([{"url": V4, "label": "agent_olive_voice_v4 · your clone", "durationHint": 7},
                                {"url": "https://h/v3/preview.wav", "label": "agent_olive_voice_v3"},
                                {"url": "s3://b/k.wav", "label": "dropped: not https"}], title="Voice audition")
    assert m["template"] == "clip-gallery" and m["title"] == "Voice audition"
    assert [(c["label"], c["kind"]) for c in m["galleryClips"]] == [
        ("agent_olive_voice_v4 · your clone", "audio"), ("agent_olive_voice_v3", "audio")]
    assert m["galleryClips"][0]["durationHint"] == 7.0 and m["id"].startswith("gallery-")


def test_videos_are_cards_and_an_all_image_set_is_an_image_gallery():
    v = build_gallery_manifest([{"url": "https://h/seg1.mp4", "label": "Segment 1", "text": "Woof!"}])
    assert v["galleryClips"][0] == {"id": "clip1", "label": "Segment 1", "src": "https://h/seg1.mp4", "kind": "video", "text": "Woof!"}
    i = build_gallery_manifest([{"url": "https://h/a.png", "label": "A"}, {"url": "https://h/b.jpg", "label": "B"}])
    assert i["template"] == "image-annotated" and [x["label"] for x in i["images"]] == ["A", "B"]


def test_the_tool_writes_stabilizes_and_announces_one_tab(monkeypatch):
    import render.artifacts as ra
    from jobs import context as jc
    from orchestration import progress_channel as pc
    from tools import media_gallery as mg
    seen = {}
    monkeypatch.setattr(jc, "current_session_id", lambda: "sess-1")
    monkeypatch.setattr(ra, "write_session_manifest", lambda sid, mid, man: (seen.update(sid=sid, man=man), f"artifacts/{sid}/render/{mid}.json")[1])
    monkeypatch.setattr(ra, "_stabilize_manifest_media_urls", lambda key, sid: seen.update(stab=(key, sid)))
    pushes = []
    class Ch:
        def push(self, status, label, agent=None, extra=None): pushes.append((status, label, agent, extra))
    monkeypatch.setattr(pc, "get_active_channel", lambda: Ch())
    out = json.loads(mg.show_media_gallery(items=[{"url": V4, "label": "agent_olive_voice_v4"}], title="Voice audition"))
    assert out == {"status": "opened", "items": 1, "template": "clip-gallery"}
    key = f"artifacts/sess-1/render/{seen['man']['id']}.json"
    assert seen["man"]["candidate_keys"]  # the original links, so the end-of-turn auto-render skips them
    assert seen["stab"] == (key, "sess-1")
    (status, label, agent, extra), = pushes
    assert extra["render_manifest_key"] == key and extra["render_label"] == "Voice audition" and agent == "render"
    assert "https://" not in json.dumps(out)  # no media url in the result: the render harvester opens nothing extra


def test_bad_input_is_explained(monkeypatch):
    from tools import media_gallery as mg
    assert "non-empty" in json.loads(mg.show_media_gallery(items=[]))["error"]
    assert "https" in json.loads(mg.show_media_gallery(items=[{"url": "s3://b/k.wav", "label": "x"}]))["error"]


def test_a_gallerys_cards_get_stable_references_too():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "render", "artifacts.py")).read()
    assert 'for field in ("galleryClips", "clips"):' in src


def test_both_agents_and_the_olive_skill_use_it():
    base = os.path.join(os.path.dirname(__file__), "..")
    for f in ("src/main.py", "src/task_agent.py"):
        assert '"show_media_gallery": show_media_gallery' in open(os.path.join(base, f)).read()
    skill = open(os.path.join(base, "skills", "agent-olive", "SKILL.md")).read()
    assert "show_media_gallery" in skill and "build_clip_gallery_manifest" not in skill
    assert "never a file id" in skill


def test_list_voice_collection_always_asks_for_the_tools_maximum_page():
    import tool_args as ta
    t = "mediaworks-m2c___text-to-speech___list_voice_collection"
    assert ta.VOICE_LIST_LIMIT == 100                                             # the schema's max
    assert ta.default_voice_listing(t, {"language": "en"}, frozenset({"limit", "language"})) == {"language": "en", "limit": 100}
    assert ta.default_voice_listing(t, {"limit": 5}, frozenset({"limit"})) == {"limit": 100}   # no pagination: raise it
    assert ta.default_voice_listing(t, {"limit": 100}, frozenset({"limit"})) == {"limit": 100}
    assert ta.default_voice_listing(t, {}, frozenset({"language"})) == {}       # schema without limit: untouched
    assert ta.default_voice_listing(t, {}, frozenset()) == {"limit": 100}        # unknown schema: still asked
    assert ta.default_voice_listing("x___get_voice", {}, frozenset({"limit"})) == {}


def test_a_full_voice_page_is_flagged_as_possibly_truncated():
    import tool_args as ta
    t = "text-to-speech___list_voice_collection"
    assert ta.voice_list_may_be_truncated(t, {"count": 100, "voices": []})
    assert not ta.voice_list_may_be_truncated(t, {"count": 54, "voices": []})
    assert ta.voice_list_may_be_truncated(t, {"voices": [{}] * 100})
    assert not ta.voice_list_may_be_truncated("x___get_voice", {"count": 100})


def test_the_wrapper_appends_a_truncation_warning_to_a_full_voice_page():
    import json as _json
    from strands.types._events import ToolResultEvent
    from jobs.strands_tool_wrap import _note_truncated_voice_list
    full = ToolResultEvent({"toolUseId": "t", "status": "success",
                            "content": [{"text": _json.dumps({"count": 100, "voices": []})}]})
    short = ToolResultEvent({"toolUseId": "t", "status": "success",
                             "content": [{"text": _json.dumps({"count": 54, "voices": []})}]})
    t = "m2c___text-to-speech___list_voice_collection"
    out = _note_truncated_voice_list(t, [full])[0]
    assert "no pagination" in out.get("tool_result")["content"][-1]["text"]
    assert _note_truncated_voice_list(t, [short])[0] is short


def test_register_voice_without_gender_is_refused_before_submission():
    import tool_args as ta
    from jobs.strands_tool_wrap import _untagged_voice_refusal
    t = "m2c___text-to-speech___register_voice"
    assert ta.register_voice_missing_gender(t, {"ref_s3uri": "s3://b/k", "ref_transcript": "hi"})
    assert ta.register_voice_missing_gender(t, {"gender": "  "})
    assert not ta.register_voice_missing_gender(t, {"gender": "female"})
    assert not ta.register_voice_missing_gender("x___synthesize_speech", {})
    refusal = _untagged_voice_refusal(t, {"toolUseId": "u1", "input": {"ref_s3uri": "s3://b/k"}})
    assert refusal["status"] == "error" and refusal["toolUseId"] == "u1"
    assert "gender" in refusal["content"][0]["text"]
    assert _untagged_voice_refusal(t, {"toolUseId": "u1", "input": {"gender": "male"}}) is None


def test_a_state_machine_arn_is_not_a_render_key():
    from hooks.provider import canonical_render_key
    assert canonical_render_key("arn:aws:states:us-west-2:1:stateMachine:m2cv6-x-tts-synthesis") == ""


def test_media_shown_in_a_named_gallery_is_never_auto_rendered_again(monkeypatch):
    import render.artifacts as ra
    ra._gallery_shown.clear()
    presigned = "https://proxy.s3.amazonaws.com/micro-workflow/tts/voices/85bc/preview.wav?X-Amz-Signature=a"
    ra.note_gallery_media([presigned])
    other_sig = "https://proxy.s3.amazonaws.com/micro-workflow/tts/voices/85bc/preview.wav?X-Amz-Signature=b"
    assert ra._shown_in_gallery(other_sig)                     # same object, a different presign
    assert not ra._shown_in_gallery("https://proxy.s3.amazonaws.com/micro-workflow/tts/voices/99aa/preview.wav")
    # a keyed render of only gallery media is dropped before any Render Agent call
    called = []
    monkeypatch.setattr(ra, "_invoke_render_agent", lambda *a, **k: called.append(1), raising=False)
    assert ra.dispatch_render([other_sig], render_key="arn_x") is None
    assert called == []
    ra._gallery_shown.clear()


def test_the_olive_skill_lists_every_voice_without_a_gender_filter():
    skill = open(os.path.join(os.path.dirname(__file__), "..", "skills", "agent-olive", "SKILL.md")).read()
    assert "no `gender` filter" in skill and "**`gender`**" in skill
    assert "limit: 200" not in skill


def test_an_s3_uri_needs_no_presign_it_becomes_the_reference_the_player_resolves(monkeypatch):
    import render.artifacts as ra
    from tools import media_gallery as mg
    monkeypatch.setattr(ra, "_SKILLS_BUCKET", "skills-bucket")
    monkeypatch.setattr(ra, "_ARTIFACTS_DOMAIN", "d12.cloudfront.net")
    assert mg._stable_src("s3://m2cv6-x-proxy/micro-workflow/tts-on-aws/voices/85bc/preview.wav") == \
        "artifacts/m2c/m2cv6-x-proxy/micro-workflow/tts-on-aws/voices/85bc/preview.wav"
    assert mg._stable_src("s3://skills-bucket/artifacts/external/a.wav") == \
        "https://d12.cloudfront.net/artifacts/external/a.wav"
    assert mg._stable_src("https://x/y.wav") == "https://x/y.wav"
    from render.gallery import build_gallery_manifest
    m = build_gallery_manifest([{"url": "artifacts/m2c/b/k/preview.wav", "label": "agent_olive_voice_v4"}])
    assert m["galleryClips"][0]["src"] == "artifacts/m2c/b/k/preview.wav"
    assert m["galleryClips"][0]["label"] == "agent_olive_voice_v4"


def test_the_olive_skill_treats_an_upload_with_a_description_as_a_reference():
    """Staging 20fd99a7: memory said the user 'reuses existing anchors' (written by test chats), so the
    agent offered the upload as-is instead of generating anchor variants from it."""
    skill = open(os.path.join(os.path.dirname(__file__), "..", "skills", "agent-olive", "SKILL.md")).read()
    assert "is a REFERENCE: generate two anchor variants" in skill
    assert "A remembered preference" in skill and "never decides this" in skill


def test_every_audio_card_gets_its_waveform_so_the_user_can_see_where_to_trim(monkeypatch):
    """Staging 20fd99a7: the named gallery replaced the Render Agent's timeline but carried no peaks, so
    an audio card was a bare player with nothing to set In / Out by."""
    import tools.peak_extractor as pe
    import render.artifacts as ra
    from tools import media_gallery as mg
    seen = []

    def fake_extractor(url, sps=100):
        seen.append(url)
        return json.dumps({"peaks": [0.1, 0.8, 0.3] * 500, "duration": 7.0})

    class _S3:
        def generate_presigned_url(self, op, Params, ExpiresIn):
            return f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}?sig=1"

    monkeypatch.setattr(pe, "peak_extractor", fake_extractor)
    monkeypatch.setattr(ra, "_s3_client", lambda: _S3())
    manifest = {"galleryClips": [
        {"id": "clip1", "kind": "audio", "src": "artifacts/m2c/m2c-proxy/micro-workflow/tts-on-aws/voices/a/preview.wav"},
        {"id": "clip2", "kind": "video", "src": "https://cdn.example/v.mp4"},
        {"id": "clip3", "kind": "audio", "src": "https://cdn.example/chunk.wav", "peaks": [0.5]},
    ]}
    mg._add_waveforms(manifest)
    c1, c2, c3 = manifest["galleryClips"]
    assert len(c1["peaks"]) == mg.MAX_PEAKS                      # resampled, not 1500 floats
    assert "peaks" not in c2                                      # video: none
    assert c3["peaks"] == [0.5]                                   # already had them: untouched
    assert seen == ["https://m2c-proxy.s3.amazonaws.com/micro-workflow/tts-on-aws/voices/a/preview.wav?sig=1"]


def test_a_card_whose_waveform_cannot_be_read_still_plays(monkeypatch):
    import tools.peak_extractor as pe
    from tools import media_gallery as mg
    monkeypatch.setattr(pe, "peak_extractor", lambda url, sps=100: json.dumps({"error": {"code": "CODEC_ERROR"}}))
    manifest = {"galleryClips": [{"id": "c", "kind": "audio", "src": "https://cdn.example/a.mp3"}]}
    mg._add_waveforms(manifest)
    assert "peaks" not in manifest["galleryClips"][0]


def test_one_video_opens_in_the_video_view_not_a_one_card_gallery(monkeypatch):
    """c7d66001's final reel came up as a "Review & clip takes" gallery with one card."""
    import render.artifacts as ra
    from jobs import context as jc
    from tools import media_gallery as mg
    monkeypatch.setattr(jc, "current_session_id", lambda: "sess-1")
    rendered, written = [], []
    monkeypatch.setattr(ra, "dispatch_render", lambda urls, label=None, **k: (rendered.append((urls, label)), "k")[1])
    monkeypatch.setattr(ra, "rendered_this_session", lambda url: False)
    monkeypatch.setattr(ra, "write_session_manifest", lambda *a: written.append(a) or "key")
    out = json.loads(mg.show_media_gallery(items=[{"url": "https://cdn/x/final.mp4", "label": "Final"}], title="Reel"))
    assert out == {"status": "opened", "items": 1, "template": "video"}
    assert rendered == [(["https://cdn/x/final.mp4"], "Reel")] and written == []
    # Two videos, or one audio file, are still a gallery.
    monkeypatch.setattr(ra, "_stabilize_manifest_media_urls", lambda *a: None)
    out = json.loads(mg.show_media_gallery(items=[{"url": "https://cdn/a.mp4", "label": "A"},
                                                  {"url": "https://cdn/b.mp4", "label": "B"}]))
    assert out["template"] == "clip-gallery" and len(rendered) == 1



def test_a_video_already_open_in_its_tab_is_not_reopened_as_a_gallery(monkeypatch):
    """a3b0a2c1: the plan had rendered the final reel in a video tab; show_media_gallery's render was deduped and it
    fell back to a one-card gallery, a second tab of the same video."""
    import render.artifacts as ra
    from jobs import context as jc
    from tools import media_gallery as mg
    monkeypatch.setattr(jc, "current_session_id", lambda: "sess-1")
    written = []
    monkeypatch.setattr(ra, "rendered_this_session", lambda url: True)
    monkeypatch.setattr(ra, "dispatch_render", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no render")))
    monkeypatch.setattr(ra, "write_session_manifest", lambda *a: written.append(a) or "key")
    out = json.loads(mg.show_media_gallery(items=[{"url": "https://cdn/x/final.mp4", "label": "Final"}]))
    assert out == {"status": "already_open", "items": 1, "template": "video"} and written == []
