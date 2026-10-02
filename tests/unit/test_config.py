from clipforge.config import Settings


def test_provider_order_parsing():
    s = Settings(llm_providers=" gemini, groq ,, openrouter ")
    assert s.provider_order == ["gemini", "groq", "openrouter"]


def test_ensure_dirs_creates_tree(tmp_path):
    s = Settings(data_dir=tmp_path / "data")
    s.ensure_dirs()
    assert s.downloads_dir.is_dir()
    assert s.clips_dir.is_dir()
    assert s.db_path.parent.is_dir()
