

def test_a_render_nobody_named_is_named_after_its_file():
    """8e2acba8: three recast results arrived as three "Render Output" tabs."""
    from render.artifacts import _file_label
    assert _file_label(["https://cdn/x/artifacts/external/fa9aebb5.mp4?X-Amz-Signature=s"]) == "fa9aebb5.mp4"
    assert _file_label(["https://cdn/a/final%20reel.mp4", "https://cdn/b.mp4"]) == "final reel.mp4 +1"
    assert _file_label([]) is None
