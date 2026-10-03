from clipforge.pipeline import download as dl


def test_subtitle_langs_uses_video_language():
    assert dl._subtitle_langs({"language": "fr-FR"}) == ["fr"]
    assert dl._subtitle_langs({}) == ["en", "fr"]


def test_subtitle_failure_is_not_fatal(tmp_path, monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            raise dl.yt_dlp.utils.DownloadError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", Boom)
    assert dl.fetch_subtitles("https://y/x", tmp_path, {}) is None
